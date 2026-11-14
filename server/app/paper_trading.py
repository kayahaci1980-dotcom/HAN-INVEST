import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, Body, HTTPException

from app.crypto import scan_crypto
from app.bybit import scan_bybit
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
_LOSS_COOLDOWN_SECONDS = 3 * 60 * 60
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
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'LONG'")
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
        position_columns = {row[1] for row in db.execute("PRAGMA table_info(paper_positions)").fetchall()}
        if "direction" not in position_columns:
            db.execute("ALTER TABLE paper_positions ADD COLUMN direction TEXT NOT NULL DEFAULT 'LONG'")
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
        trades = db.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 30"
        ).fetchall()
        # Totals use the complete ledger; the UI history remains capped at 30 rows.
        realized_rows = db.execute(
            "SELECT pnl_usdt FROM paper_trades WHERE pnl_usdt IS NOT NULL"
        ).fetchall()

    cash = float(account["cash"])
    holdings = 0.0
    realized_profit = sum(max(0.0, float(row["pnl_usdt"])) for row in realized_rows)
    realized_loss = sum(max(0.0, -float(row["pnl_usdt"])) for row in realized_rows)
    unrealized_profit = 0.0
    unrealized_loss = 0.0
    open_position_items = []

    for position in positions:
        quantity = float(position["quantity"])
        entry = float(position["entry_price"])
        price = float(position["last_price"])
        cost = float(position["cost_usdt"])
        direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()

        if direction == "SHORT":
            # Reserve is returned as collateral, then mark-to-market the short
            # and deduct both the opening fee already paid and estimated close fee.
            opening_fee = max(0.0, cost - entry * quantity)
            closing_fee = price * quantity * _FEE_RATE
            market_value = cost + (entry - price) * quantity - opening_fee - closing_fee
            unrealized = (entry - price) * quantity - opening_fee - closing_fee
        else:
            market_value = price * quantity
            unrealized = market_value * (1.0 - _FEE_RATE) - cost

        holdings += market_value
        unrealized_profit += max(0.0, unrealized)
        unrealized_loss += max(0.0, -unrealized)
        item = dict(position)
        item["side"] = direction
        item["market_value_usdt"] = market_value
        item["unrealized_pnl_usdt"] = unrealized
        open_position_items.append(item)

    equity = cash + holdings
    net_pnl = equity - float(account["initial_cash"])
    profit = realized_profit + unrealized_profit
    loss = realized_loss + unrealized_loss

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
        "realized_pnl_usdt": realized_profit - realized_loss,
        "unrealized_pnl_usdt": unrealized_profit - unrealized_loss,
        "total_pnl_usdt": net_pnl,
        "open_positions": open_position_items,
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
    direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()
    entry = float(position["entry_price"])
    if direction == "SHORT":
        opening_fee = float(position["cost_usdt"]) - entry * quantity
        pnl = (entry - price) * quantity - opening_fee - fee
        cash_release = entry * quantity + (entry - price) * quantity - fee
        trade_side = "SHORT_COVER"
    else:
        net = gross - fee
        pnl = net - float(position["cost_usdt"])
        cash_release = net
        trade_side = "SELL"
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
            "Loss cooldown started for %s on %s: %.2f%%; 3 hours",
            position["symbol"], position["exchange"], loss_pct,
        )
    account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
    db.execute(
        "UPDATE paper_account SET cash=?, updated_at=? WHERE id=1",
        (float(account["cash"]) + cash_release, _now()),
    )
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), position["exchange"], position["symbol"], trade_side, price,
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
    if exists:
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


def _open_short_position(db, exchange: str, asset: dict[str, Any], reason: str) -> bool:
    symbol = str(asset.get("symbol") or "")
    try:
        price = float(asset.get("price") or 0)
        atr = float(asset.get("atr_14") or asset.get("atr_14_1d") or 0)
    except (TypeError, ValueError):
        return False
    if not symbol or price <= 0 or atr <= 0:
        return False
    if db.execute("SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                  (exchange, symbol)).fetchone():
        return False
    cash = float(db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
    stop, target = price * 1.02, price * 0.90
    risk_per_coin = (stop - price) + (price + stop) * _FEE_RATE
    if cash <= 0 or risk_per_coin <= 0:
        return False
    qty = min(cash * _RISK_PCT / 100.0 / risk_per_coin,
              cash / (price * (1 + _FEE_RATE)))
    if qty <= 0:
        return False
    gross, fee = qty * price, qty * price * _FEE_RATE
    reserve = gross + fee
    if reserve > cash + 1e-8:
        return False
    db.execute("""INSERT INTO paper_positions
        (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
         take_profit, last_price, opened_at, direction)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SHORT')""",
        (exchange, symbol, qty, reserve, price, stop, target, price, _now()))
    db.execute("UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1",
               (reserve, _now()))
    db.execute("""INSERT INTO paper_trades
        (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
        VALUES (?, ?, ?, 'SHORT_OPEN', ?, ?, ?, ?, NULL, ?)""",
        (_now(), exchange, symbol, price, qty, gross, fee, reason))
    return True


def _apply_market_data(
    exchange: str, payload: dict[str, Any], allow_new_entries: bool = False,
    allow_short_entries: bool = False,
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
            direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()
            change_pct = (((entry - price) / entry) if direction == "SHORT" else (price / entry - 1.0)) * 100.0
            if direction == "SHORT":
                if change_pct >= 10.0:
                    _close_position(db, position, price, "SHORT_TAKE_PROFIT_10")
                elif change_pct <= -2.0:
                    _close_position(db, position, price, "SHORT_STOP_LOSS_2")
            else:
                if change_pct >= 10.0:
                    _close_position(db, position, price, "TAKE_PROFIT_10_REBUY")
                elif score <= 82:
                    _close_position(db, position, price, "SCORE_AT_OR_BELOW_82")
                elif change_pct <= -2.0:
                    _close_position(db, position, price, "DRAWDOWN_2_PERCENT")

        # Market breadth blocks only new entries. Position exits above always run.
        if not allow_new_entries and not allow_short_entries:
            return

        current_positions = db.execute("SELECT COUNT(*) AS n FROM paper_positions").fetchone()["n"]
        if current_positions >= _MAX_POSITIONS:
            return
        candidates = sorted((x for x in assets if isinstance(x, dict)),
                            key=lambda x: float(x.get("score") or 0), reverse=True)
        for asset in candidates:
            if current_positions >= _MAX_POSITIONS:
                break
            try:
                score = float(asset.get("score") or 0)
                price = float(asset.get("price") or 0)
                ma20 = float(asset.get("ma20") or asset.get("ema20") or 0)
                ma50 = float(asset.get("ma50") or 0)
            except (TypeError, ValueError):
                continue
            if asset.get("trade_ready") is not True or not asset.get("symbol"):
                continue
            cooldown = db.execute("SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?",
                                  (exchange, str(asset["symbol"]))).fetchone()
            if cooldown and float(cooldown["blocked_until"]) > _now().timestamp():
                continue
            opened = False
            if allow_new_entries and score >= 83:
                opened = _open_position(db, exchange, asset, "AUTO_LONG_SCORE_83_PLUS")
            elif allow_short_entries and score <= 65 and price > 0 and ma20 > 0 and ma50 > 0 and price < ma20 <= ma50:
                opened = _open_short_position(db, exchange, asset, "AUTO_SHORT_BEARISH_SCORE_65_OR_BELOW")
            if opened:
                current_positions += 1


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
                scan_coinbase(limit=50, min_volume=1_000_000),
                scan_okx(limit=50, min_volume=1_000_000),
                return_exceptions=True,
            )
            breadth_result, *results = gathered
            errors = []
            if isinstance(breadth_result, BaseException) or not isinstance(breadth_result, dict):
                allow_new_entries = False
                allow_short_entries = False
                _last_market_breadth = {
                    "available": False, "status": "UNAVAILABLE",
                    "allow_new_entries": False,
                    "reason": "Нет достоверных глобальных данных; новые входы заблокированы.",
                }
                errors.append("Market breadth: unavailable; new entries blocked")
                logger.warning("Market breadth unavailable; fail-closed entry filter active")
            else:
                _last_market_breadth = dict(breadth_result)
                allow_new_entries = breadth_result.get("allow_new_entries") is True
                allow_short_entries = breadth_result.get("available") is True
                logger.info(
                    "Market breadth %s: rising=%s falling=%s total=%s; new entries=%s",
                    breadth_result.get("status"), breadth_result.get("rising_coins"),
                    breadth_result.get("falling_coins"), breadth_result.get("total_coins"),
                    "allowed" if allow_new_entries else "blocked",
                )
            # All four exchanges provide market analysis, but Bybit Spot is the
            # ONLY exchange allowed to open or close paper-trading positions.
            exchange_payloads = dict(zip(
                ("Binance", "Bybit", "Coinbase Exchange", "OKX"), results
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
                _apply_market_data("Bybit", bybit_payload, allow_new_entries=allow_new_entries,
                                   allow_short_entries=allow_short_entries)
            else:
                errors.append("Bybit: trading skipped because Bybit market data is unavailable")
            coinbase_payload = exchange_payloads.get("Coinbase Exchange")
            if isinstance(coinbase_payload, dict):
                _apply_market_data("Coinbase Exchange", coinbase_payload, allow_new_entries=allow_new_entries,
                                   allow_short_entries=allow_short_entries)
            else:
                errors.append("Coinbase Exchange: trading skipped because market data is unavailable")
            logger.info("Paper trading venues: Bybit Spot and Coinbase Exchange; virtual LONG/SHORT")
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
async def sell_paper_position(payload: dict[str, Any] = Body(...)):
    """Manually close one server-side virtual LONG or SHORT position."""
    exchange = str(payload.get("exchange") or "").strip()
    symbol = str(payload.get("symbol") or "").strip()
    requested_side = str(payload.get("side") or "").strip().upper()

    if not exchange or not symbol:
        raise HTTPException(status_code=422, detail="exchange and symbol are required")
    if requested_side not in ("LONG", "SHORT"):
        raise HTTPException(status_code=422, detail="side must be LONG or SHORT")

    async with _cycle_lock:
        _init_db()
        with _connect() as db:
            position = db.execute(
                "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                (exchange, symbol),
            ).fetchone()
            if position is None:
                raise HTTPException(status_code=404, detail="Open paper position not found")
            actual_side = str(
                position["direction"] if "direction" in position.keys() else "LONG"
            ).upper()
            if actual_side != requested_side:
                raise HTTPException(status_code=409, detail="Position direction does not match")
            price = float(position["last_price"])
            if price <= 0:
                raise HTTPException(status_code=409, detail="No valid latest price for this position")
            _close_position(db, position, price, "MANUAL_CLOSE")
    return _account_snapshot()
