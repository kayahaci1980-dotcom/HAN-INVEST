import asyncio
import logging
import math
import os
import sqlite3

import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, HTTPException

from app.crypto import scan_crypto
from app.bybit import scan_bybit, scan_bybit_short
from app.exchange_scanners import scan_coinbase, scan_okx
from app.market_breadth import get_market_breadth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/paper", tags=["paper-trading"])

# Supabase PostgreSQL is the durable store. SQLite remains a local/test fallback only.
_DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/tmp/han-invest-paper.sqlite3"))
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_STORAGE_PERSISTENT = bool(_DATABASE_URL or os.getenv("PAPER_DB_PATH"))
_INITIAL_CASH = 1000.0
_FEE_RATE = 0.001
_RISK_PCT = 1.0
_MAX_POSITIONS = 10
_SHORT_MIN_SCORE = 65
_LOSS_COOLDOWN_SECONDS = 8 * 60 * 60
_SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("PAPER_SCAN_INTERVAL_SECONDS", "300")))
_worker_task: asyncio.Task | None = None
_scan_requested = asyncio.Event()
_cycle_lock = asyncio.Lock()
_last_market_breadth: dict[str, Any] = {
    "available": False, "status": "NOT_SCANNED", "allow_new_entries": False
}


class _PostgresCompatConnection:
    """Small adapter that keeps the existing SQLite-style query code portable."""
    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    def execute(self, query: str, parameters: tuple = ()):
        return self.connection.execute(query.replace("?", "%s"), parameters)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)


def _connect():
    if _DATABASE_URL:
        connection = psycopg.connect(
            _DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=10,
            sslmode="require",
        )
        return _PostgresCompatConnection(connection)

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(_DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _now():
    return datetime.now(timezone.utc)


def _init_db() -> None:
    if _DATABASE_URL:
        # Tables are provisioned by the reviewed Supabase migration.
        with _connect() as db:
            db.execute(
                """INSERT INTO paper_account
                   (id, initial_cash, cash, enabled, updated_at)
                   VALUES (1, ?, ?, FALSE, ?)
                   ON CONFLICT (id) DO NOTHING""",
                (_INITIAL_CASH, _INITIAL_CASH, _now()),
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS paper_short_positions (
                    exchange TEXT NOT NULL, symbol TEXT NOT NULL,
                    quantity DOUBLE PRECISION NOT NULL, margin_usdt DOUBLE PRECISION NOT NULL,
                    entry_fee_usdt DOUBLE PRECISION NOT NULL, entry_price DOUBLE PRECISION NOT NULL,
                    stop_loss DOUBLE PRECISION NOT NULL, take_profit DOUBLE PRECISION NOT NULL,
                    last_price DOUBLE PRECISION NOT NULL, opened_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (exchange, symbol)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS paper_loss_cooldowns (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    blocked_until DOUBLE PRECISION NOT NULL,
                    loss_pct DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (exchange, symbol)
                )"""
            )
        return

    # Local/test fallback; never treated as durable on Render unless PAPER_DB_PATH is set.
    with _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS paper_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            initial_cash REAL NOT NULL,
            cash REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            last_scan_at TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_positions (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            cost_usdt REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            last_price REAL NOT NULL,
            opened_at TEXT NOT NULL,
            PRIMARY KEY (exchange, symbol)
        );
        CREATE TABLE IF NOT EXISTS paper_short_positions (
            exchange TEXT NOT NULL, symbol TEXT NOT NULL,
            quantity REAL NOT NULL, margin_usdt REAL NOT NULL,
            entry_fee_usdt REAL NOT NULL, entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL, take_profit REAL NOT NULL,
            last_price REAL NOT NULL, opened_at TEXT NOT NULL,
            PRIMARY KEY (exchange, symbol)
        );
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            value_usdt REAL NOT NULL,
            fee_usdt REAL NOT NULL,
            pnl_usdt REAL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_loss_cooldowns (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            blocked_until REAL NOT NULL,
            loss_pct REAL NOT NULL,
            PRIMARY KEY (exchange, symbol)
        );
        """)
        db.execute(
            """INSERT OR IGNORE INTO paper_account
               (id, initial_cash, cash, enabled, updated_at)
               VALUES (1, ?, ?, 0, ?)""",
            (_INITIAL_CASH, _INITIAL_CASH, _now()),
        )


def _account_snapshot() -> dict[str, Any]:
    _init_db()
    with _connect() as db:
        account = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        positions = db.execute(
            "SELECT * FROM paper_positions ORDER BY opened_at DESC"
        ).fetchall()
        shorts = db.execute(
            "SELECT * FROM paper_short_positions ORDER BY opened_at DESC"
        ).fetchall()
        trades = db.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 30"
        ).fetchall()

    cash = float(account["cash"])
    fee_rate = float(_FEE_RATE)

    # Closed-trade P/L is aggregated over the full ledger, not only the
    # latest 30 rows returned to the UI.
    with _connect() as db:
        realized_rows = db.execute(
            "SELECT pnl_usdt FROM paper_trades WHERE pnl_usdt IS NOT NULL"
        ).fetchall()

    realized_profit = sum(max(0.0, float(row["pnl_usdt"])) for row in realized_rows)
    realized_loss = sum(max(0.0, -float(row["pnl_usdt"])) for row in realized_rows)

    long_positions = []
    short_positions = []
    holdings = 0.0
    unrealized_profit = 0.0
    unrealized_loss = 0.0

    for p in positions:
        quantity = float(p["quantity"])
        last_price = float(p["last_price"])
        cost = float(p["cost_usdt"])
        market_value = quantity * last_price
        unrealized = market_value - cost
        holdings += market_value
        unrealized_profit += max(0.0, unrealized)
        unrealized_loss += max(0.0, -unrealized)
        long_positions.append(dict(p, side="LONG", market_value_usdt=market_value,
                                   unrealized_pnl_usdt=unrealized))

    for p in shorts:
        quantity = float(p["quantity"])
        entry = float(p["entry_price"])
        last_price = float(p["last_price"])
        margin = float(p["margin_usdt"])
        entry_fee = float(p["entry_fee_usdt"])
        exit_fee_estimate = quantity * last_price * fee_rate
        unrealized = quantity * (entry - last_price) - entry_fee - exit_fee_estimate
        # Cash was reduced by margin + entry fee at entry. Add back margin,
        # mark-to-market P/L and estimated exit fee to obtain equity.
        market_value = margin + quantity * (entry - last_price) - exit_fee_estimate
        holdings += market_value
        unrealized_profit += max(0.0, unrealized)
        unrealized_loss += max(0.0, -unrealized)
        short_positions.append(dict(
            p, side="SHORT",
            cost_usdt=margin + entry_fee,
            market_value_usdt=market_value,
            estimated_exit_fee_usdt=exit_fee_estimate,
            unrealized_pnl_usdt=unrealized,
        ))

    profit = realized_profit + unrealized_profit
    loss = realized_loss + unrealized_loss
    equity = cash + holdings
    net_pnl = profit - loss

    return {
        "mode": "PAPER",
        "enabled": bool(account["enabled"]),
        "initial_cash_usdt": float(account["initial_cash"]),
        "cash_usdt": cash,
        "holdings_value_usdt": holdings,
        "equity_usdt": equity,
        "profit_usdt": profit,
        "loss_usdt": loss,
        "net_pnl_usdt": net_pnl,
        "realized_profit_usdt": realized_profit,
        "realized_loss_usdt": realized_loss,
        "unrealized_profit_usdt": unrealized_profit,
        "unrealized_loss_usdt": unrealized_loss,
        "open_positions": long_positions + short_positions,
        "history": [dict(t) for t in trades],
        "last_scan_at": account["last_scan_at"],
        "last_error": account["last_error"],
        "market_breadth": dict(_last_market_breadth),
        "storage": "supabase_postgres" if _DATABASE_URL else ("persistent_disk" if _STORAGE_PERSISTENT else "temporary_container_storage"),
        "storage_persistent_configured": _STORAGE_PERSISTENT,
        "scan_interval_seconds": _SCAN_INTERVAL_SECONDS,
        "fee_rate_per_side": _FEE_RATE,
        "risk_pct_per_trade": _RISK_PCT,
        "max_open_positions": _MAX_POSITIONS,
        "notice": (
            "Виртуальная Spot-торговля. Реальные ордера на биржи не отправляются. "
            "Счёт сохраняется в Supabase PostgreSQL." if _DATABASE_URL else
            "Виртуальная Spot-торговля. Реальные ордера на биржи не отправляются. "
            "Для сохранения счёта настройте DATABASE_URL (Supabase PostgreSQL) или постоянный диск. "
            "Бесплатный Render может засыпать; круглосуточная работа не гарантируется."
        ),
    }


def _close_position(db, position,
                    price: float, reason: str) -> None:
    quantity = float(position["quantity"])
    gross = quantity * price
    fee = gross * _FEE_RATE
    net = gross - fee
    pnl = net - float(position["cost_usdt"])
    cost_basis = float(position["cost_usdt"])
    loss_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
    if loss_pct <= -2.0:
        blocked_until = _now().timestamp() + _LOSS_COOLDOWN_SECONDS
        db.execute(
            """INSERT INTO paper_loss_cooldowns
               (exchange, symbol, blocked_until, loss_pct)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (exchange, symbol) DO UPDATE SET
                   blocked_until=excluded.blocked_until,
                   loss_pct=excluded.loss_pct""",
            (position["exchange"], position["symbol"], blocked_until, loss_pct),
        )
        logger.info(
            "Loss cooldown started for %s on %s: %.2f%%; 8 hours",
            position["symbol"], position["exchange"], loss_pct,
        )
    account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
    db.execute(
        "UPDATE paper_account SET cash=?, updated_at=? WHERE id=1",
        (float(account["cash"]) + net, _now()),
    )
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?)""",
        (_now(), position["exchange"], position["symbol"], price,
         quantity, gross, fee, pnl, reason),
    )
    db.execute(
        "DELETE FROM paper_positions WHERE exchange=? AND symbol=?",
        (position["exchange"], position["symbol"]),
    )


def _open_position(db, exchange: str,
                   asset: dict[str, Any], reason: str) -> bool:
    """Open a virtual spot position using ATR risk sizing."""
    symbol = str(asset.get("symbol") or "")
    try:
        price = float(asset.get("price") or 0)
        atr = float(asset.get("atr_14") or asset.get("atr_14_1d") or 0)
    except (TypeError, ValueError):
        return False
    if not symbol or price <= 0 or atr <= 0:
        return False

    exists = db.execute(
        "SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
        (exchange, symbol),
    ).fetchone()
    short_exists = db.execute(
        "SELECT 1 FROM paper_short_positions WHERE exchange=? AND symbol=?",
        (exchange, symbol),
    ).fetchone()
    if exists or short_exists:
        return False

    account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
    cash = float(account["cash"])
    # ATR controls position sizing; user-defined exits are fixed at -2% and +10%.
    atr_risk_stop = price - 1.5 * atr
    stop = price * 0.98
    target = price * 1.10
    risk_per_coin = (price - atr_risk_stop) + (price + atr_risk_stop) * _FEE_RATE
    if cash <= 0 or atr_risk_stop <= 0 or stop <= 0 or target <= price or risk_per_coin <= 0:
        return False

    risk_budget = cash * _RISK_PCT / 100.0
    quantity_by_risk = risk_budget / risk_per_coin
    quantity_by_cash = cash / (price * (1 + _FEE_RATE))
    quantity = min(quantity_by_risk, quantity_by_cash)
    if quantity <= 0:
        return False

    gross = quantity * price
    fee = gross * _FEE_RATE
    total_cost = gross + fee
    if total_cost > cash + 1e-8:
        return False

    db.execute(
        """INSERT INTO paper_positions
           (exchange, symbol, quantity, cost_usdt, entry_price,
            stop_loss, take_profit, last_price, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (exchange, symbol, quantity, total_cost, price,
         stop, target, price, _now()),
    )
    db.execute(
        "UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1",
        (total_cost, _now()),
    )
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, NULL, ?)""",
        (_now(), exchange, symbol, price, quantity, gross, fee, reason),
    )
    return True


def _apply_market_data(
    exchange: str, payload: dict[str, Any], allow_new_entries: bool = False
) -> None:
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        return
    by_symbol = {
        str(asset.get("symbol")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("symbol")
    }

    with _connect() as db:
        account = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if not account or not account["enabled"]:
            return

        # Exit rules are evaluated before new entries. Missing/stale prices never trade.
        open_positions = db.execute(
            "SELECT * FROM paper_positions WHERE exchange=?", (exchange,)
        ).fetchall()
        for position in open_positions:
            asset = by_symbol.get(position["symbol"])
            if not asset:
                continue
            try:
                price = float(asset.get("price") or 0)
                score = float(asset.get("score") or 0)
                entry = float(position["entry_price"])
            except (TypeError, ValueError):
                continue
            if price <= 0 or entry <= 0:
                continue

            db.execute(
                """UPDATE paper_positions SET last_price=?
                   WHERE exchange=? AND symbol=?""",
                (price, exchange, position["symbol"]),
            )
            change_pct = (price / entry - 1.0) * 100.0

            # +10% has highest priority: always take profit; re-buy only when the
            # market-breadth gate allows new entries. The next scan applies normal exits.
            if change_pct >= 10.0:
                _close_position(db, position, price, "TAKE_PROFIT_10_REBUY")
                cooldown = db.execute(
                    """SELECT blocked_until FROM paper_loss_cooldowns
                       WHERE exchange=? AND symbol=?""",
                    (exchange, position["symbol"]),
                ).fetchone()
                cooldown_active = (
                    cooldown is not None
                    and float(cooldown["blocked_until"]) > _now().timestamp()
                )
                if (
                    allow_new_entries
                    and not cooldown_active
                    and score >= 83
                    and asset.get("trade_ready") is True
                    and _open_position(db, exchange, asset, "AUTO_REBUY_AFTER_10_PERCENT")
                ):
                    logger.info("Paper re-bought %s on %s after +10%%", position["symbol"], exchange)
            elif score <= 82:
                _close_position(db, position, price, "SCORE_AT_OR_BELOW_82")
            elif change_pct <= -2.0:
                _close_position(db, position, price, "DRAWDOWN_2_PERCENT")

        # Market breadth blocks only new entries. Position exits above always run.
        if not allow_new_entries:
            return

        # Automatic entries require a score of at least 83; no BUY signal or bullish BTC filter is required.
        current_positions = db.execute(
            "SELECT (SELECT COUNT(*) FROM paper_positions) + (SELECT COUNT(*) FROM paper_short_positions) AS n"
        ).fetchone()["n"]
        if current_positions >= _MAX_POSITIONS:
            return

        candidates = sorted(
            (asset for asset in assets if isinstance(asset, dict)),
            key=lambda item: float(item.get("score") or 0),
            reverse=True,
        )
        for asset in candidates:
            if current_positions >= _MAX_POSITIONS:
                break
            try:
                score = float(asset.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if (
                score < 83
                or asset.get("trade_ready") is not True
                or not asset.get("symbol")
            ):
                continue
            cooldown = db.execute(
                """SELECT blocked_until FROM paper_loss_cooldowns
                   WHERE exchange=? AND symbol=?""",
                (exchange, str(asset["symbol"])),
            ).fetchone()
            if cooldown and float(cooldown["blocked_until"]) > _now().timestamp():
                continue
            if _open_position(db, exchange, asset, "AUTO_SCORE_83_PLUS"):
                current_positions += 1


def _close_short_position(db, position, price: float, reason: str) -> None:
    qty = float(position['quantity'])
    margin = float(position['margin_usdt'])
    entry = float(position['entry_price'])
    entry_fee = float(position['entry_fee_usdt'])
    gross_pnl = qty * (entry - price)
    exit_fee = qty * price * _FEE_RATE
    pnl = gross_pnl - entry_fee - exit_fee
    loss_pct = pnl / margin * 100.0 if margin > 0 else 0.0
    if loss_pct <= -2.0:
        db.execute('''INSERT INTO paper_loss_cooldowns (exchange, symbol, blocked_until, loss_pct)
                      VALUES (?, ?, ?, ?) ON CONFLICT (exchange, symbol) DO UPDATE SET
                      blocked_until=excluded.blocked_until, loss_pct=excluded.loss_pct''',
                   (position['exchange'], position['symbol'], _now().timestamp() + _LOSS_COOLDOWN_SECONDS, loss_pct))
    account = db.execute('SELECT cash FROM paper_account WHERE id=1').fetchone()
    db.execute('UPDATE paper_account SET cash=?, updated_at=? WHERE id=1',
               (float(account['cash']) + margin + gross_pnl - exit_fee, _now()))
    db.execute('''INSERT INTO paper_trades
                 (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
                 VALUES (?, ?, ?, 'SHORT_CLOSE', ?, ?, ?, ?, ?, ?)''',
               (_now(), position['exchange'], position['symbol'], price, qty, qty * price, exit_fee, pnl, reason))
    db.execute('DELETE FROM paper_short_positions WHERE exchange=? AND symbol=?',
               (position['exchange'], position['symbol']))


def _open_short_position(db, exchange: str, asset: dict[str, Any], reason: str) -> bool:
    symbol = str(asset.get('symbol') or '')
    try:
        price = float(asset.get('price') or 0)
    except (TypeError, ValueError):
        return False
    if not symbol or price <= 0 or asset.get('short_ready') is not True:
        return False
    if db.execute('SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?', (exchange, symbol)).fetchone():
        return False
    if db.execute('SELECT 1 FROM paper_short_positions WHERE exchange=? AND symbol=?', (exchange, symbol)).fetchone():
        return False
    cash = float(db.execute('SELECT cash FROM paper_account WHERE id=1').fetchone()['cash'])
    risk_budget = cash * _RISK_PCT / 100.0
    risk_per_coin = price * 0.02 + price * 1.98 * _FEE_RATE
    if cash <= 0 or risk_per_coin <= 0:
        return False
    qty = min(risk_budget / risk_per_coin, cash / (price * (1 + _FEE_RATE)))
    margin = qty * price
    entry_fee = margin * _FEE_RATE
    if qty <= 0 or margin + entry_fee > cash:
        return False
    db.execute('''INSERT INTO paper_short_positions
                 (exchange, symbol, quantity, margin_usdt, entry_fee_usdt, entry_price,
                  stop_loss, take_profit, last_price, opened_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
               (exchange, symbol, qty, margin, entry_fee, price, price * 1.02, price * 0.90, price, _now()))
    db.execute('UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1', (margin + entry_fee, _now()))
    db.execute('''INSERT INTO paper_trades
                 (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
                 VALUES (?, ?, ?, 'SHORT_OPEN', ?, ?, ?, ?, NULL, ?)''',
               (_now(), exchange, symbol, price, qty, margin, entry_fee, reason))
    return True


def _apply_short_market_data(payload: dict[str, Any], allow_new_shorts: bool = False) -> None:
    assets = payload.get('assets') or []
    if not isinstance(assets, list):
        return
    by_symbol = {str(a.get('symbol')): a for a in assets if isinstance(a, dict) and a.get('symbol')}
    with _connect() as db:
        account = db.execute('SELECT * FROM paper_account WHERE id=1').fetchone()
        if not account or not account['enabled']:
            return
        positions = db.execute("SELECT * FROM paper_short_positions WHERE exchange='Bybit'").fetchall()
        for position in positions:
            asset = by_symbol.get(position['symbol'])
            if not asset:
                continue
            try:
                price = float(asset.get('price') or 0)
                score = float(asset.get('short_score') or 0)
                entry = float(position['entry_price'])
            except (TypeError, ValueError):
                continue
            if price <= 0 or entry <= 0:
                continue
            db.execute("UPDATE paper_short_positions SET last_price=? WHERE exchange='Bybit' AND symbol=?",
                       (price, position['symbol']))
            favorable = (entry / price - 1.0) * 100.0
            if favorable >= 10.0:
                _close_short_position(db, position, price, 'SHORT_TAKE_PROFIT_10')
                cooldown = db.execute('SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?',
                                      ('Bybit', position['symbol'])).fetchone()
                blocked = cooldown is not None and float(cooldown['blocked_until']) > _now().timestamp()
                if allow_new_shorts and not blocked and _open_short_position(db, 'Bybit', asset, 'AUTO_SHORT_REOPEN_AFTER_10_PERCENT'):
                    logger.info('Paper re-opened SHORT %s after 10%% favorable move', position['symbol'])
            elif score < _SHORT_MIN_SCORE:
                _close_short_position(db, position, price, 'SHORT_SCORE_BELOW_65')
            elif favorable <= -2.0:
                _close_short_position(db, position, price, 'SHORT_STOP_LOSS_2_PERCENT')
        if not allow_new_shorts:
            return
        count = db.execute('SELECT (SELECT COUNT(*) FROM paper_positions) + (SELECT COUNT(*) FROM paper_short_positions) AS n').fetchone()['n']
        if count >= _MAX_POSITIONS:
            return
        candidates = sorted((a for a in assets if isinstance(a, dict)),
                            key=lambda a: float(a.get('short_score') or 0), reverse=True)
        for asset in candidates:
            if count >= _MAX_POSITIONS:
                break
            try:
                score = float(asset.get('short_score') or 0)
            except (TypeError, ValueError):
                continue
            if score < _SHORT_MIN_SCORE or asset.get('short_ready') is not True or not asset.get('symbol'):
                continue
            cooldown = db.execute('SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?',
                                  ('Bybit', str(asset['symbol']))).fetchone()
            if cooldown and float(cooldown['blocked_until']) > _now().timestamp():
                continue
            if _open_short_position(db, 'Bybit', asset, 'AUTO_SHORT_SCORE_83_PLUS'):
                count += 1

async def _scan_cycle() -> None:
    global _last_market_breadth
    async with _cycle_lock:
        try:
            # Each exchange is scanned independently. All requests are public market data only.
            gathered = await asyncio.gather(
                get_market_breadth(),
                scan_crypto(limit=50, universe_limit=200, min_volume=1_000_000,
                            capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                            fee_rate=_FEE_RATE),
                scan_bybit(limit=50, min_volume=1_000_000,
                           capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                           fee_rate=_FEE_RATE),
                scan_bybit_short(limit=50, min_volume=1_000_000),
                scan_coinbase(limit=50, min_volume=1_000_000),
                scan_okx(limit=50, min_volume=1_000_000),
                return_exceptions=True,
            )
            breadth_result, *results = gathered
            errors = []
            allow_new_shorts = False
            if isinstance(breadth_result, BaseException) or not isinstance(breadth_result, dict):
                allow_new_entries = False
                allow_new_shorts = False
                _last_market_breadth = {
                    "available": False, "status": "UNAVAILABLE",
                    "allow_new_entries": False,
                    "reason": "Нет достоверных глобальных данных; новые входы заблокированы.",
                }
                errors.append("Market breadth: unavailable; new entries blocked")
                logger.warning("Market breadth unavailable; fail-closed entry filter active")
            else:
                _last_market_breadth = dict(breadth_result)
                allow_new_entries = breadth_result.get("allow_new_longs", breadth_result.get("allow_new_entries")) is True
                allow_new_shorts = breadth_result.get("allow_new_shorts") is True
                logger.info(
                    "Market breadth %s: rising=%s falling=%s total=%s; new entries=%s",
                    breadth_result.get("status"), breadth_result.get("rising_coins"),
                    breadth_result.get("falling_coins"), breadth_result.get("total_coins"),
                    "allowed" if allow_new_entries else "blocked",
                )
            # All four exchanges provide market analysis, but Bybit Spot is the
            # ONLY exchange allowed to open or close paper-trading positions.
            exchange_payloads = dict(zip(
                ("Binance", "Bybit", "Bybit SHORT", "Coinbase Exchange", "OKX"), results
            ))
            successes = 0
            for exchange, payload in exchange_payloads.items():
                if isinstance(payload, BaseException):
                    errors.append(exchange + ": " + type(payload).__name__)
                elif not isinstance(payload, dict):
                    errors.append(exchange + ": invalid response")
                else:
                    successes += 1

            bybit_payload = exchange_payloads.get("Bybit")
            if isinstance(bybit_payload, dict):
                _apply_market_data("Bybit", bybit_payload, allow_new_entries=allow_new_entries)
            else:
                errors.append("Bybit Spot: LONG trading skipped because market data is unavailable")
            short_payload = exchange_payloads.get("Bybit SHORT")
            if isinstance(short_payload, dict):
                _apply_short_market_data(short_payload, allow_new_shorts=allow_new_shorts)
            else:
                errors.append("Bybit Perpetual: SHORT trading skipped because market data is unavailable")
            logger.info(
                "Paper trading venues: Bybit Spot LONG and Bybit USDT Perpetual SHORT (virtual 1x); other exchanges analysis-only"
            )
            with _connect() as db:
                db.execute(
                    """UPDATE paper_account SET last_scan_at=?, last_error=?, updated_at=?
                       WHERE id=1""",
                    (_now(), "; ".join(errors) if errors else None, _now()),
                )
            if successes == 0:
                logger.warning("HAN INVEST paper scan failed: %s", "; ".join(errors))
        except Exception as exc:
            logger.exception("HAN INVEST paper trading cycle failed")
            with _connect() as db:
                db.execute(
                    "UPDATE paper_account SET last_error=?, updated_at=? WHERE id=1",
                    (type(exc).__name__, _now()),
                )


async def _worker() -> None:
    _init_db()
    while True:
        try:
            with _connect() as db:
                account = db.execute(
                    "SELECT enabled FROM paper_account WHERE id=1"
                ).fetchone()
            if account and account["enabled"]:
                await _scan_cycle()
            try:
                await asyncio.wait_for(
                    _scan_requested.wait(), timeout=_SCAN_INTERVAL_SECONDS
                )
                _scan_requested.clear()
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HAN INVEST paper worker loop error")
            await asyncio.sleep(30)


def start_paper_worker() -> None:
    global _worker_task
    _init_db()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def stop_paper_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


@router.get("/state")
async def paper_state():
    return _account_snapshot()


@router.post("/enable")
async def enable_paper_trading():
    if not _STORAGE_PERSISTENT:
        raise HTTPException(
            status_code=409,
            detail=(
                "Автоматический режим не включён: сначала подключите Supabase PostgreSQL "
                "через DATABASE_URL или настройте постоянный диск."
            ),
        )
    _init_db()
    with _connect() as db:
        db.execute(
            "UPDATE paper_account SET enabled=TRUE, updated_at=?, last_error=NULL WHERE id=1",
            (_now(),),
        )
    _scan_requested.set()
    return _account_snapshot()


@router.post("/disable")
async def disable_paper_trading():
    _init_db()
    with _connect() as db:
        db.execute(
            "UPDATE paper_account SET enabled=FALSE, updated_at=? WHERE id=1",
            (_now(),),
        )
    return _account_snapshot()


@router.post("/sell")
async def sell_paper_position(payload: dict[str, Any]):
    """Manually close a virtual Bybit LONG Spot or SHORT perpetual position."""
    exchange = str(payload.get("exchange") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "LONG").strip().upper()
    if exchange not in {"bybit", "bybit spot", "bybit usdt perpetual"}:
        raise HTTPException(status_code=400, detail="Разрешено ручное закрытие только виртуальных позиций Bybit.")
    if side not in {"LONG", "SHORT"} or not symbol.endswith("USDT") or not symbol.replace("USDT", "").isalnum():
        raise HTTPException(status_code=400, detail="Некорректная сторона или USDT-символ.")
    async with _cycle_lock:
        with _connect() as db:
            if side == "SHORT":
                position = db.execute("SELECT * FROM paper_short_positions WHERE exchange=? AND symbol=?", ("Bybit", symbol)).fetchone()
                category = "linear"
            else:
                position = db.execute("SELECT * FROM paper_positions WHERE exchange=? AND symbol=?", ("Bybit", symbol)).fetchone()
                category = "spot"
            if not position:
                raise HTTPException(status_code=404, detail="Открытая позиция не найдена.")
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get("https://api.bybit.com/v5/market/tickers", params={"category": category, "symbol": symbol})
                    response.raise_for_status()
                    data = response.json()
                if data.get("retCode") != 0:
                    raise ValueError("Bybit ticker unavailable")
                rows = data.get("result", {}).get("list", [])
                if not rows:
                    raise ValueError("Bybit ticker unavailable")
                price = float(rows[0].get("lastPrice") or 0)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("Invalid Bybit price")
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                raise HTTPException(status_code=503, detail="Не удалось получить актуальную цену Bybit. Позиция оставлена открытой.") from exc
            if side == "SHORT":
                _close_short_position(db, position, price, "MANUAL_CLOSE_SHORT")
            else:
                _close_position(db, position, price, "MANUAL_SELL")
    return _account_snapshot()
