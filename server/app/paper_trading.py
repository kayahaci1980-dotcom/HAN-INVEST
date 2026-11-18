import asyncio
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, Body, HTTPException

from app.bybit import fetch_spot_ticker_prices, scan_bybit
from app.market_breadth import get_market_breadth
from app.ai_advisor import get_ai_advisor_state, refresh_ai_exit_advisor

logger = logging.getLogger(__name__)

# Stablecoin base assets are excluded from directional PAPER trading.
_STABLECOIN_BASES = {
    "USDT", "USDC", "USDE", "USDS", "DAI", "FDUSD", "TUSD", "USDP",
    "PYUSD", "USD1", "RLUSD", "UST", "USTC", "EURC", "EURI", "USDD",
    "FRAX", "USDX", "GUSD",
}

def _is_stablecoin_pair(symbol: str) -> bool:
    clean = str(symbol or "").upper().replace("-", "").replace("/", "").replace("_", "")
    for quote in ("USDT", "USDC", "USD"):
        if clean.endswith(quote):
            base = clean[:-len(quote)]
            return base in _STABLECOIN_BASES
    return clean in _STABLECOIN_BASES

router = APIRouter(prefix="/api/v1/paper", tags=["paper-trading"])

# Supabase PostgreSQL is the durable store. SQLite remains a local/test fallback only.
_DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/tmp/han-invest-paper.sqlite3"))
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_STORAGE_PERSISTENT = bool(_DATABASE_URL or os.getenv("PAPER_DB_PATH"))
_INITIAL_CASH = 1000.0
_FEE_RATE = 0.001
_MAX_POSITION_LOSS_USDT = 1.0
_RISK_PCT = 1.0
_MAX_POSITIONS = 20
_STOP_LOSS_COOLDOWN_SECONDS = 4 * 60 * 60
_TAKE_PROFIT_COOLDOWN_SECONDS = 1 * 60 * 60
_TRAILING_STOP_COOLDOWN_SECONDS = 10 * 60
_CONSECUTIVE_LOSSES_COOLDOWN_SECONDS = 8 * 60 * 60
_SCAN_INTERVAL_SECONDS = max(10, int(os.getenv("PAPER_SCAN_INTERVAL_SECONDS", "10")))
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
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS next_add_level INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS next_reduce_level INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS peak_price DOUBLE PRECISION NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS initial_risk DOUBLE PRECISION NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS atr_14_1h DOUBLE PRECISION NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS trailing_stage INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS trailing_candle TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS peak_profit_usdt DOUBLE PRECISION NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS loss_started_at DOUBLE PRECISION")
            db.execute(
                """CREATE TABLE IF NOT EXISTS paper_position_lots (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    lot_index INTEGER NOT NULL,
                    quantity DOUBLE PRECISION NOT NULL,
                    cost_usdt DOUBLE PRECISION NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    opened_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (exchange, symbol, lot_index)
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
            db.execute(
                """INSERT INTO paper_position_lots
                   (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
                   SELECT p.exchange, p.symbol, 0, p.quantity, p.cost_usdt, p.entry_price, p.opened_at
                   FROM paper_positions p
                   WHERE UPPER(COALESCE(p.direction, 'LONG'))='LONG'
                     AND NOT EXISTS (
                       SELECT 1 FROM paper_position_lots l
                       WHERE l.exchange=p.exchange AND l.symbol=p.symbol
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
        CREATE TABLE IF NOT EXISTS paper_position_lots (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            lot_index INTEGER NOT NULL,
            quantity REAL NOT NULL,
            cost_usdt REAL NOT NULL,
            entry_price REAL NOT NULL,
            opened_at TEXT NOT NULL,
            PRIMARY KEY (exchange, symbol, lot_index)
        );
        """)
        position_columns = {row[1] for row in db.execute("PRAGMA table_info(paper_positions)").fetchall()}
        if "direction" not in position_columns:
            db.execute("ALTER TABLE paper_positions ADD COLUMN direction TEXT NOT NULL DEFAULT 'LONG'")
        if "next_add_level" not in position_columns:
            db.execute("ALTER TABLE paper_positions ADD COLUMN next_add_level INTEGER NOT NULL DEFAULT 1")
        if "next_reduce_level" not in position_columns:
            db.execute("ALTER TABLE paper_positions ADD COLUMN next_reduce_level INTEGER NOT NULL DEFAULT 1")
        if "peak_price" not in position_columns:
            db.execute("ALTER TABLE paper_positions ADD COLUMN peak_price REAL NOT NULL DEFAULT 0")
        for column, definition in (
            ("initial_risk", "REAL NOT NULL DEFAULT 0"),
            ("atr_14_1h", "REAL NOT NULL DEFAULT 0"),
            ("trailing_stage", "INTEGER NOT NULL DEFAULT 0"),
            ("trailing_candle", "TEXT NOT NULL DEFAULT ''"),
            ("peak_profit_usdt", "REAL NOT NULL DEFAULT 0"),
            ("loss_started_at", "REAL"),
        ):
            if column not in position_columns:
                db.execute(f"ALTER TABLE paper_positions ADD COLUMN {column} {definition}")
        db.execute(
            """INSERT OR IGNORE INTO paper_position_lots
               (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
               SELECT p.exchange, p.symbol, 0, p.quantity, p.cost_usdt, p.entry_price, p.opened_at
               FROM paper_positions p
               WHERE UPPER(COALESCE(p.direction, 'LONG'))='LONG'
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_position_lots l
                   WHERE l.exchange=p.exchange AND l.symbol=p.symbol
                 )"""
        )
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
        trade_rows = db.execute(
            "SELECT * FROM paper_trades ORDER BY id ASC LIMIT 2000"
        ).fetchall()
        lot_rows = db.execute("SELECT * FROM paper_position_lots").fetchall()
        lots_by_position = {}
        for lot in lot_rows:
            lots_by_position.setdefault((lot["exchange"], lot["symbol"]), []).append(lot)
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
        # Keep the displayed entry price consistent with the PnL: scale-in lots
        # can have different fills, while position.entry_price remains the first fill.
        lots = lots_by_position.get((position["exchange"], position["symbol"]), [])
        base_entry = entry
        if lots:
            lots_quantity = sum(float(lot["quantity"]) for lot in lots)
            average_entry = (
                sum(float(lot["entry_price"]) * float(lot["quantity"]) for lot in lots)
                / lots_quantity
                if lots_quantity > 0 else base_entry
            )
        else:
            average_entry = base_entry

        if direction == "SHORT":
            # Calculate short PnL per lot: add-ons can have different entry prices.
            if lots:
                opening_fee = sum(max(0.0, float(lot["cost_usdt"]) -
                                      float(lot["entry_price"]) * float(lot["quantity"]))
                                  for lot in lots)
                gross_pnl = sum((float(lot["entry_price"]) - price) * float(lot["quantity"])
                                for lot in lots)
            else:
                # Backward-compatible fallback for legacy positions without lot rows.
                opening_fee = max(0.0, cost - entry * quantity)
                gross_pnl = (entry - price) * quantity
            closing_fee = price * quantity * _FEE_RATE
            market_value = cost + gross_pnl - opening_fee - closing_fee
            unrealized = gross_pnl - opening_fee - closing_fee
        else:
            # Equity uses estimated liquidation value, including the closing fee.
            market_value = price * quantity * (1.0 - _FEE_RATE)
            unrealized = market_value - cost

        holdings += market_value
        unrealized_profit += max(0.0, unrealized)
        unrealized_loss += max(0.0, -unrealized)
        item = dict(position)
        item["base_entry_price"] = base_entry
        item["average_entry_price"] = average_entry
        item["entry_price"] = average_entry
        item["side"] = direction
        item["market_value_usdt"] = market_value
        item["unrealized_pnl_usdt"] = unrealized
        open_position_items.append(item)

    equity = cash + holdings
    net_pnl = equity - float(account["initial_cash"])
    profit = realized_profit + unrealized_profit
    loss = realized_loss + unrealized_loss

    # Build readable round-trip timestamps from the immutable trade ledger.
    # Scale-ins do not reset the base opening time; partial scale-outs do not close a cycle.
    open_times: dict[tuple[str, str], Any] = {}
    close_labels = {
        "STOP_LOSS": "Стоп-лосс",
        "TRAILING_STOP": "Трейлинг-стоп",
        "TAKE_PROFIT": "Тейк-профит",
        "MANUAL_CLOSE": "Ручное закрытие",
    }
    enriched_trades = []
    for trade_row in trade_rows:
        trade = dict(trade_row)
        key = (str(trade["exchange"]), str(trade["symbol"]))
        reason = str(trade.get("reason") or "")
        side = str(trade.get("side") or "")
        event_time = trade.get("time")
        is_base_open = (
            side in {"BUY", "SHORT_OPEN"}
            and reason.startswith(("AUTO_LONG_", "AUTO_SHORT_"))
        )
        if is_base_open and key not in open_times:
            open_times[key] = event_time
        trade["opened_at"] = open_times.get(key)
        trade["closed_at"] = None
        trade["close_reason_label"] = None
        if reason in close_labels and side in {"SELL", "SHORT_COVER"}:
            trade["closed_at"] = event_time
            trade["close_reason_label"] = close_labels[reason]
            open_times.pop(key, None)
        enriched_trades.append(trade)
    history = list(reversed(enriched_trades[-30:]))

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
        "history": history,
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
def _loss_limit_stop_price(db, position) -> float:
    """Price that limits net loss, including entry and estimated exit fees, to a fixed 1 USDT."""
    quantity = float(position["quantity"] or 0)
    cost = float(position["cost_usdt"] or 0)
    entry = float(position["entry_price"] or 0)
    direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()
    if quantity <= 0 or cost <= 0 or entry <= 0:
        return 0.0

    if direction == "SHORT":
        lots = db.execute(
            "SELECT quantity, cost_usdt, entry_price FROM paper_position_lots WHERE exchange=? AND symbol=?",
            (position["exchange"], position["symbol"]),
        ).fetchall()
        if lots:
            entry_notional = sum(float(lot["entry_price"]) * float(lot["quantity"]) for lot in lots)
            opening_fee = sum(max(0.0, float(lot["cost_usdt"]) -
                                  float(lot["entry_price"]) * float(lot["quantity"])) for lot in lots)
        else:
            entry_notional = entry * quantity
            opening_fee = max(0.0, cost - entry_notional)
        # Net SHORT PnL = entry notional - cover notional - both fees.
        return max(0.0, (entry_notional - opening_fee + _MAX_POSITION_LOSS_USDT) /
                   (quantity * (1.0 + _FEE_RATE)))

    # Net LONG PnL = sale proceeds after exit fee - entry cost (incl. entry fee).
    return max(0.0, (cost - _MAX_POSITION_LOSS_USDT) /
               (quantity * (1.0 - _FEE_RATE)))


def _close_position(db, position,
                    price: float, reason: str) -> None:
    quantity = float(position["quantity"])
    gross = quantity * price
    fee = gross * _FEE_RATE
    direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()
    entry = float(position["entry_price"])
    if direction == "SHORT":
        lots = db.execute(
            "SELECT quantity, cost_usdt, entry_price FROM paper_position_lots WHERE exchange=? AND symbol=?",
            (position["exchange"], position["symbol"]),
        ).fetchall()
        if lots:
            opening_fee = sum(max(0.0, float(lot["cost_usdt"]) -
                                  float(lot["entry_price"]) * float(lot["quantity"]))
                              for lot in lots)
            gross_pnl = sum((float(lot["entry_price"]) - price) * float(lot["quantity"])
                            for lot in lots)
        else:
            opening_fee = max(0.0, float(position["cost_usdt"]) - entry * quantity)
            gross_pnl = (entry - price) * quantity
        pnl = gross_pnl - opening_fee - fee
        cash_release = float(position["cost_usdt"]) + pnl
        trade_side = "SHORT_COVER"
    else:
        net = gross - fee
        pnl = net - float(position["cost_usdt"])
        cash_release = net
        trade_side = "SELL"
    cost_basis = float(position["cost_usdt"])
    loss_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
    cooldown_seconds = {
        "STOP_LOSS": _STOP_LOSS_COOLDOWN_SECONDS,
        "TAKE_PROFIT": _TAKE_PROFIT_COOLDOWN_SECONDS,
        "TRAILING_STOP": _TRAILING_STOP_COOLDOWN_SECONDS,
    }.get(reason, 0)
    if cooldown_seconds:
        blocked_until = _now().timestamp() + cooldown_seconds
        db.execute(
            """INSERT INTO paper_loss_cooldowns
               (exchange, symbol, blocked_until, loss_pct)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (exchange, symbol) DO UPDATE SET
                   blocked_until=excluded.blocked_until,
                   loss_pct=excluded.loss_pct""",
            (position["exchange"], position["symbol"], blocked_until, loss_pct),
        )
        logger.info("%s cooldown started for %s on %s: %s seconds",
                    reason, position["symbol"], position["exchange"], cooldown_seconds)
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
    # Count fully closed trades for this exact exchange+symbol only.
    # Other symbols must never contribute to this coin's losing streak.
    recent_closes = db.execute(
        """SELECT pnl_usdt FROM paper_trades
           WHERE exchange=? AND symbol=? AND pnl_usdt IS NOT NULL
             AND reason IN ('STOP_LOSS', 'TAKE_PROFIT', 'TRAILING_STOP', 'MANUAL_CLOSE')
           ORDER BY id DESC LIMIT 2""",
        (position["exchange"], position["symbol"]),
    ).fetchall()
    if len(recent_closes) == 2 and all(float(row["pnl_usdt"]) < 0 for row in recent_closes):
        symbol_until = _now().timestamp() + _CONSECUTIVE_LOSSES_COOLDOWN_SECONDS
        db.execute(
            """INSERT INTO paper_loss_cooldowns
               (exchange, symbol, blocked_until, loss_pct)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (exchange, symbol) DO UPDATE SET
                   blocked_until=excluded.blocked_until, loss_pct=excluded.loss_pct""",
            (position["exchange"], position["symbol"], symbol_until, loss_pct),
        )
        logger.warning("Two consecutive losing closes: %s on %s blocked for 8 hours",
                       position["symbol"], position["exchange"])
    db.execute(
        "DELETE FROM paper_position_lots WHERE exchange=? AND symbol=?",
        (position["exchange"], position["symbol"]),
    )
    db.execute(
        "DELETE FROM paper_positions WHERE exchange=? AND symbol=?",
        (position["exchange"], position["symbol"]),
    )


def _open_position(db, exchange: str, asset: dict[str, Any], reason: str) -> bool:
    """Open a virtual LONG with a fixed 100 USDT base tranche."""
    symbol = str(asset.get("symbol") or "")
    try:
        price = float(asset.get("price") or 0)
        atr = float(asset.get("atr_14_1h") or 0)
        candle_time = str(asset.get("atr_candle_time") or "")
    except (TypeError, ValueError):
        return False
    if not symbol or price <= 0 or atr <= 0:
        return False
    if db.execute("SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                  (exchange, symbol)).fetchone():
        return False
    cash = float(db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
    ticket = min(100.0, cash)
    # No fixed price target: the approved ATR trailing rules manage exits.
    # TAKE_PROFIT cooldown remains available for a separately defined TP event.
    stop, target = price - 1.5 * atr, 0.0
    if ticket < 10 or stop <= 0 or not candle_time:
        return False
    quantity = ticket / (price * (1.0 + _FEE_RATE))
    gross, fee = quantity * price, quantity * price * _FEE_RATE
    cost = gross + fee
    db.execute("""INSERT INTO paper_positions
        (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
         take_profit, last_price, opened_at, peak_price, direction,
         initial_risk, atr_14_1h, trailing_stage, trailing_candle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LONG', ?, ?, 0, ?)""",
        (exchange, symbol, quantity, cost, price, stop, target, price, _now(), price, 1.5 * atr, atr, candle_time))
    db.execute("UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1", (cost, _now()))
    db.execute("""INSERT INTO paper_position_lots
        (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
        VALUES (?, ?, 0, ?, ?, ?, ?)""",
        (exchange, symbol, quantity, cost, price, _now()))
    db.execute("""INSERT INTO paper_trades
        (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
        VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, NULL, ?)""",
        (_now(), exchange, symbol, price, quantity, gross, fee, reason))
    return True


def _add_scale_lot(db, position, price: float) -> bool:
    """Buy one add-on tranche equal to the original entry tranche."""
    exchange, symbol = position["exchange"], position["symbol"]
    base = db.execute(
        """SELECT cost_usdt FROM paper_position_lots
           WHERE exchange=? AND symbol=? AND lot_index=0""",
        (exchange, symbol),
    ).fetchone()
    if not base:
        return False
    tranche_cost = float(base["cost_usdt"])
    account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
    if tranche_cost <= 0 or float(account["cash"]) + 1e-8 < tranche_cost:
        return False
    quantity = tranche_cost / (price * (1.0 + _FEE_RATE))
    gross = quantity * price
    fee = gross * _FEE_RATE
    actual_cost = gross + fee
    latest = db.execute(
        """SELECT COALESCE(MAX(lot_index), 0) AS n FROM paper_position_lots
           WHERE exchange=? AND symbol=?""",
        (exchange, symbol),
    ).fetchone()
    lot_index = int(latest["n"]) + 1
    db.execute(
        """INSERT INTO paper_position_lots
           (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (exchange, symbol, lot_index, quantity, actual_cost, price, _now()),
    )
    db.execute(
        """UPDATE paper_positions
           SET quantity=quantity+?, cost_usdt=cost_usdt+?, last_price=?,
               next_add_level=next_add_level+1
           WHERE exchange=? AND symbol=?""",
        (quantity, actual_cost, price, exchange, symbol),
    )
    db.execute(
        "UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1",
        (actual_cost, _now()),
    )
    direction = str(position["direction"] if "direction" in position.keys() else "LONG").upper()
    side = "SHORT_OPEN" if direction == "SHORT" else "BUY"
    reason = ("SHORT_SCALE_IN_MINUS_{}_PERCENT".format(int(position["next_add_level"]))
              if direction == "SHORT"
              else "SCALE_IN_PLUS_{}_PERCENT".format(int(position["next_add_level"])))
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
        (_now(), exchange, symbol, side, price, quantity, gross, fee, reason),
    )
    return True


def _reduce_latest_scale_lot(db, position, price: float) -> bool:
    """Sell only the most recently added tranche; the base lot is never reduced."""
    exchange, symbol = position["exchange"], position["symbol"]
    lot = db.execute(
        """SELECT * FROM paper_position_lots
           WHERE exchange=? AND symbol=? AND lot_index>0
           ORDER BY lot_index DESC LIMIT 1""",
        (exchange, symbol),
    ).fetchone()
    if not lot:
        return False
    quantity = float(lot["quantity"])
    cost = float(lot["cost_usdt"])
    gross = quantity * price
    fee = gross * _FEE_RATE
    proceeds = gross - fee
    pnl = proceeds - cost
    account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
    db.execute(
        "UPDATE paper_account SET cash=cash+?, updated_at=? WHERE id=1",
        (proceeds, _now()),
    )
    db.execute(
        """UPDATE paper_positions
           SET quantity=quantity-?, cost_usdt=cost_usdt-?, last_price=?,
               next_reduce_level=next_reduce_level+1
           WHERE exchange=? AND symbol=?""",
        (quantity, cost, price, exchange, symbol),
    )
    db.execute(
        "DELETE FROM paper_position_lots WHERE exchange=? AND symbol=? AND lot_index=?",
        (exchange, symbol, lot["lot_index"]),
    )
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?)""",
        (_now(), exchange, symbol, price, quantity, gross, fee, pnl,
         "SCALE_OUT_PULLBACK_{}_PERCENT".format(int(position["next_reduce_level"]))),
    )
    return True


def _manage_long_scale(db, position, price: float) -> None:
    """Apply the hard stop, add at +1% from base entry, and reduce on 1% pullbacks from peak."""
    entry = float(position["entry_price"])
    if entry <= 0 or price <= 0:
        return
    quantity = float(position["quantity"])
    cost = float(position["cost_usdt"])
    net_liquidation = quantity * price * (1.0 - _FEE_RATE)
    loss_pct = ((net_liquidation - cost) / cost * 100.0) if cost > 0 else 0.0
    peak = max(float(position["peak_price"] or entry), price)
    db.execute(
        "UPDATE paper_positions SET peak_price=? WHERE exchange=? AND symbol=?",
        (peak, position["exchange"], position["symbol"]),
    )
    position = db.execute(
        "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
        (position["exchange"], position["symbol"]),
    ).fetchone()
    if position is None:
        return

    # Add milestones remain anchored to the original purchase price.
    next_add = int(position["next_add_level"] if "next_add_level" in position.keys() else 1)
    while price >= entry * (1.0 + next_add / 100.0):
        if not _add_scale_lot(db, position, price):
            break
        next_add += 1
        position = db.execute(
            "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
            (position["exchange"], position["symbol"]),
        ).fetchone()

    next_reduce = int(position["next_reduce_level"] if "next_reduce_level" in position.keys() else 1)
    while price <= entry * (1.0 - next_reduce / 100.0):
        if not _reduce_latest_scale_lot(db, position, price):
            break
        next_reduce += 1
        position = db.execute(
            "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
            (position["exchange"], position["symbol"]),
        ).fetchone()
        if position is None:
            break


def _open_short_position(db, exchange: str, asset: dict[str, Any], reason: str) -> bool:
    """Open a virtual SHORT with a fixed 100 USDT collateral tranche."""
    symbol = str(asset.get("symbol") or "")
    try:
        price = float(asset.get("price") or 0)
        atr = float(asset.get("atr_14_1h") or 0)
        candle_time = str(asset.get("atr_candle_time") or "")
    except (TypeError, ValueError):
        return False
    if not symbol or price <= 0 or atr <= 0:
        return False
    if db.execute("SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                  (exchange, symbol)).fetchone():
        return False
    cash = float(db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
    ticket = min(100.0, cash)
    # No fixed price target: the approved ATR trailing rules manage exits.
    stop, target = price + 1.5 * atr, 0.0
    if ticket < 10 or atr <= 0 or not candle_time:
        return False
    quantity = ticket / (price * (1.0 + _FEE_RATE))
    gross, fee = quantity * price, quantity * price * _FEE_RATE
    reserve = gross + fee
    db.execute("""INSERT INTO paper_positions
        (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
         take_profit, last_price, opened_at, direction, peak_price,
         initial_risk, atr_14_1h, trailing_stage, trailing_candle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SHORT', ?, ?, ?, 0, ?)""",
        (exchange, symbol, quantity, reserve, price, stop, target, price, _now(), price, 1.5 * atr, atr, candle_time))
    db.execute("UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1", (reserve, _now()))
    db.execute("""INSERT INTO paper_position_lots
        (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
        VALUES (?, ?, 0, ?, ?, ?, ?)""",
        (exchange, symbol, quantity, reserve, price, _now()))
    db.execute("""INSERT INTO paper_trades
        (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
        VALUES (?, ?, ?, 'SHORT_OPEN', ?, ?, ?, ?, NULL, ?)""",
        (_now(), exchange, symbol, price, quantity, gross, fee, reason))
    return True


def _manage_short_scale(db, position, price: float) -> None:
    """Add on each favorable 1% SHORT move; reduce newest add-on on each adverse 1%."""
    entry = float(position["entry_price"])
    if entry <= 0 or price <= 0:
        return
    next_add = int(position["next_add_level"] or 1)
    while price <= entry * (1.0 - next_add / 100.0):
        if not _add_scale_lot(db, position, price):
            break
        next_add += 1
        position = db.execute("SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                              (position["exchange"], position["symbol"])).fetchone()
        if position is None:
            return
    next_reduce = int(position["next_reduce_level"] or 1)
    while price >= entry * (1.0 + next_reduce / 100.0):
        lot = db.execute("""SELECT * FROM paper_position_lots
            WHERE exchange=? AND symbol=? AND lot_index>0
            ORDER BY lot_index DESC LIMIT 1""",
            (position["exchange"], position["symbol"])).fetchone()
        if lot is None:
            break
        qty, lot_cost = float(lot["quantity"]), float(lot["cost_usdt"])
        lot_entry = float(lot["entry_price"])
        fee = qty * price * _FEE_RATE
        pnl = (lot_entry - price) * qty - max(0.0, lot_cost - lot_entry * qty) - fee
        release = lot_entry * qty + (lot_entry - price) * qty - fee
        db.execute("UPDATE paper_account SET cash=cash+?, updated_at=? WHERE id=1", (release, _now()))
        db.execute("""UPDATE paper_positions SET quantity=quantity-?, cost_usdt=cost_usdt-?,
            next_reduce_level=next_reduce_level+1, last_price=?
            WHERE exchange=? AND symbol=?""",
            (qty, lot_cost, price, position["exchange"], position["symbol"]))
        db.execute("DELETE FROM paper_position_lots WHERE exchange=? AND symbol=? AND lot_index=?",
                   (position["exchange"], position["symbol"], lot["lot_index"]))
        db.execute("""INSERT INTO paper_trades
            (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
            VALUES (?, ?, ?, 'SHORT_COVER', ?, ?, ?, ?, ?, ?)""",
            (_now(), position["exchange"], position["symbol"], price, qty, qty * price, fee, pnl,
             "SHORT_SCALE_OUT_ADVERSE_{}_PERCENT".format(next_reduce)))
        next_reduce += 1
        position = db.execute("SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                              (position["exchange"], position["symbol"])).fetchone()
        if position is None:
            return



def _apply_market_data(
    exchange: str, payload: dict[str, Any], allow_new_entries: bool = False,
    allow_short_entries: bool = False, allow_long_entries: bool = False,
    position_prices: dict[str, float] | None = None,
    ai_advisor: dict[str, Any] | None = None,
) -> None:
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        return
    logger.info(
        "PAPER scan input exchange=%s assets=%d trade_ready=%d",
        exchange, len(assets),
        sum(1 for item in assets if isinstance(item, dict) and item.get("trade_ready") is True),
    )
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
        live_position_prices = position_prices or {}
        for position in open_positions:
            asset = by_symbol.get(position["symbol"])
            try:
                # Prefer a fresh direct Bybit ticker quote. The analysis universe is
                # intentionally capped, but protective exits must cover every open position.
                price = float(live_position_prices.get(position["symbol"]) or
                              ((asset or {}).get("price") or 0))
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
            current = db.execute(
                "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                (exchange, position["symbol"]),
            ).fetchone()
            if current is None:
                continue
            quantity = float(current["quantity"])
            cost = float(current["cost_usdt"])
            if direction == "SHORT":
                opening_fee = max(0.0, cost - entry * quantity)
                net_pnl = (entry - price) * quantity - opening_fee - price * quantity * _FEE_RATE
            else:
                net_pnl = quantity * price * (1.0 - _FEE_RATE) - cost
            loss_pct = (net_pnl / cost * 100.0) if cost > 0 else 0.0
            initial_risk = float(current["initial_risk"] or 0)
            atr_1h = float((asset or {}).get("atr_14_1h") or current["atr_14_1h"] or 0)
            candle_time = str((asset or {}).get("atr_candle_time") or "")
            stop = float(current["stop_loss"] or 0)
            # Hard account-level loss cap: net realized PnL at the stop includes
            # both entry and estimated exit fees and cannot exceed 1% of position cost.
            loss_limit_stop = _loss_limit_stop_price(db, current)
            protected_stop = (
                max(stop, loss_limit_stop) if direction == "LONG"
                else min(stop, loss_limit_stop) if stop > 0
                else loss_limit_stop
            )
            if protected_stop > 0 and abs(protected_stop - stop) > 1e-12:
                stop = protected_stop
                db.execute(
                    "UPDATE paper_positions SET stop_loss=? WHERE exchange=? AND symbol=?",
                    (stop, exchange, position["symbol"]),
                )
                current = db.execute(
                    "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                    (exchange, position["symbol"]),
                ).fetchone()
            # Legacy positions may still carry the former unapproved fixed +2R TP.
            # Disable it so it cannot cut off the +2R/+3R trailing stages.
            if float(current["take_profit"] or 0) != 0:
                db.execute(
                    "UPDATE paper_positions SET take_profit=0 WHERE exchange=? AND symbol=?",
                    (exchange, position["symbol"]),
                )
                current = db.execute(
                    "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                    (exchange, position["symbol"]),
                ).fetchone()
            target = 0.0

            # Profit-based trailing stop: retain the highest net PnL reached.
            # The stop is exactly 1 USDT below that peak and is evaluated on every
            # fresh quote, independently of the 1H analysis candle.
            lots = db.execute(
                "SELECT quantity, cost_usdt, entry_price FROM paper_position_lots WHERE exchange=? AND symbol=?",
                (exchange, position["symbol"]),
            ).fetchall()
            if direction == "SHORT":
                if lots:
                    opening_fee = sum(max(0.0, float(lot["cost_usdt"]) -
                                          float(lot["entry_price"]) * float(lot["quantity"]))
                                      for lot in lots)
                    gross_pnl = sum((float(lot["entry_price"]) - price) * float(lot["quantity"])
                                    for lot in lots)
                else:
                    opening_fee = max(0.0, cost - entry * quantity)
                    gross_pnl = (entry - price) * quantity
                net_pnl = gross_pnl - opening_fee - price * quantity * _FEE_RATE
            else:
                net_pnl = quantity * price * (1.0 - _FEE_RATE) - cost

            extreme = float(current["peak_price"] or entry)
            extreme = max(extreme, price) if direction == "LONG" else min(extreme, price)
            peak_profit = max(float(current["peak_profit_usdt"] or 0.0), net_pnl)
            db.execute(
                """UPDATE paper_positions
                   SET peak_price=?, peak_profit_usdt=?
                   WHERE exchange=? AND symbol=?""",
                (extreme, peak_profit, exchange, position["symbol"]),
            )
            current = db.execute(
                "SELECT * FROM paper_positions WHERE exchange=? AND symbol=?",
                (exchange, position["symbol"]),
            ).fetchone()

            # Keep the existing hard loss protection separate from profit trailing.
            stop = float(current["stop_loss"] or 0)
            if stop > 0 and (price <= stop if direction == "LONG" else price >= stop):
                _close_position(
                    db, current,
                    min(price, stop) if direction == "LONG" else max(price, stop),
                    "STOP_LOSS",
                )
                continue

            trailing_floor = peak_profit - 1.0
            if peak_profit > 0 and net_pnl <= trailing_floor + 1e-9:
                _close_position(db, current, price, "TRAILING_STOP")
                logger.info(
                    "PAPER %s %s closed by profit trailing stop: peak %.4f USDT, current %.4f USDT, giveback 1.00 USDT",
                    direction, position["symbol"], peak_profit, net_pnl,
                )
                continue

            if direction == "LONG":
                _manage_long_scale(db, current, price)
            else:
                _manage_short_scale(db, current, price)

        # No technical, score, volume, trend, daily-open, or breadth entry filters.
        # Operational checks and protective position sizing/stops remain mandatory.
        diagnostic = {"assets": len(assets), "candidates": 0, "opened_long": 0, "opened_short": 0,
                      "common_rejections": {}, "long_rejections": {}, "short_rejections": {},
                      "market_long_allowed": bool(allow_long_entries),
                      "market_short_allowed": bool(allow_short_entries)}
        advisor_decisions: dict[str, dict[str, Any]] = {}
        advisor_ready = ai_advisor is not None
        if advisor_ready:
            try:
                updated_at = datetime.fromisoformat(str(ai_advisor.get("updated_at") or "").replace("Z", "+00:00"))
                age_seconds = (_now() - updated_at).total_seconds()
                advisor_provider = str(ai_advisor.get("provider") or "").strip().lower()
                advisor_ready = (ai_advisor.get("available") is True and ai_advisor.get("status") == "OK"
                                 and str(ai_advisor.get("mode") or "").upper() == "SHADOW"
                                 and advisor_provider in {"groq", "google gemini", "google gemini (fallback)"}
                                 and -5 <= age_seconds <= 300)
                if advisor_ready:
                    advisor_decisions = {str(item.get("symbol") or "").upper(): item
                                         for item in ai_advisor.get("recommendations", [])
                                         if isinstance(item, dict) and item.get("symbol")}
            except (TypeError, ValueError):
                advisor_ready = False
            diagnostic["ai_gate"] = "AI_ADVISOR_READY" if advisor_ready else "AI_ADVISOR_UNAVAILABLE_OR_STALE"
            diagnostic["ai_provider"] = str(ai_advisor.get("provider") or "unknown")
            diagnostic["ai_min_confidence"] = 80
            diagnostic["ai_max_age_seconds"] = 300
        else:
            diagnostic["gemini_gate"] = "BYPASSED_BYBIT_ONLY"
        def reject(reason: str, direction: str | None = None) -> None:
            bucket = diagnostic["common_rejections"]
            bucket[reason] = bucket.get(reason, 0) + 1
            if direction:
                key = "long_rejections" if direction == "LONG" else "short_rejections"
                bucket = diagnostic[key]
                bucket[reason] = bucket.get(reason, 0) + 1
        current_positions = db.execute("SELECT COUNT(*) AS n FROM paper_positions").fetchone()["n"]
        diagnostic["open_positions"] = int(current_positions)
        diagnostic["max_positions"] = _MAX_POSITIONS
        candidates = sorted((x for x in assets if isinstance(x, dict)),
                            key=lambda x: float(x.get("quote_volume_24h") or 0), reverse=True)
        diagnostic["candidates"] = len(candidates)
        for asset in candidates:
            if current_positions >= _MAX_POSITIONS:
                diagnostic["position_limit_blocked"] = True
                break
            symbol = str(asset.get("symbol") or "").strip().upper()
            try:
                price = float(asset.get("price") or 0)
                atr = float(asset.get("atr_14_1h") or 0)
                candle_time = str(asset.get("atr_candle_time") or "")
                momentum = float(asset.get("momentum_6h_pct") or 0)
            except (TypeError, ValueError):
                reject("invalid_numeric_data")
                continue
            # Valid price and ATR candle are essential to attach the existing protective stop.
            if not symbol:
                reject("symbol_missing")
                continue
            if price <= 0 or not math.isfinite(price):
                reject("price_missing")
                continue
            if atr <= 0 or not math.isfinite(atr) or not candle_time:
                reject("protective_atr_data_missing")
                continue
            if db.execute("SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                          (exchange, symbol)).fetchone():
                reject("position_already_open")
                continue
            cooldown = db.execute("SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?",
                                  (exchange, symbol)).fetchone()
            if cooldown and float(cooldown["blocked_until"]) > _now().timestamp():
                reject("cooldown_active")
                continue
            # Production requires a fresh successful Groq or Gemini recommendation.
            # Missing, stale, failed, WAIT, or low-confidence advice blocks new entries.
            confidence = 0
            if ai_advisor is not None:
                if not advisor_ready:
                    reject("ai_advisor_unavailable_or_stale")
                    continue
                decision = advisor_decisions.get(symbol)
                if not decision:
                    reject("ai_advisor_decision_missing")
                    continue
                ai_action = str(decision.get("action") or "WAIT").upper()
                try:
                    confidence = int(decision.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0
                if ai_action not in {"LONG", "SHORT"}:
                    reject("ai_advisor_wait_or_direction_not_confirmed")
                    continue
                direction = ai_action
                if confidence < 80:
                    reject("groq_confidence_below_80", direction)
                    continue
                if direction == "LONG" and not allow_long_entries:
                    reject("long_entries_disabled", direction)
                    continue
                if direction == "SHORT" and not allow_short_entries:
                    reject("short_entries_disabled", direction)
                    continue
            else:
                # Select the stronger direction when its Bybit score reaches 70.
                try:
                    long_score = float(asset.get("long_score", 0) or 0)
                    short_score = float(asset.get("short_score", 0) or 0)
                except (TypeError, ValueError):
                    reject("directional_score_missing_or_invalid")
                    continue
                eligible = []
                if allow_long_entries and math.isfinite(long_score) and long_score >= 70:
                    eligible.append(("LONG", long_score))
                if allow_short_entries and math.isfinite(short_score) and short_score >= 70:
                    eligible.append(("SHORT", short_score))
                if not eligible:
                    reject("directional_score_below_70")
                    continue
                direction, _ = max(eligible, key=lambda item: item[1])
            score_key = "long_score" if direction == "LONG" else "short_score"
            raw_score = asset.get(score_key, asset.get("score"))
            try:
                directional_score = float(raw_score)
            except (TypeError, ValueError):
                reject("directional_score_missing_or_invalid", direction)
                continue
            if not math.isfinite(directional_score) or directional_score < 70:
                reject("directional_score_below_85", direction)
                continue
            reason = (f"AUTO_{direction}_AI_CONF_{confidence}" if ai_advisor is not None
                      else f"AUTO_{direction}_SCORE_70")
            opened = (_open_position(db, exchange, asset, reason) if direction == "LONG"
                      else _open_short_position(db, exchange, asset, reason))
            if opened:
                diagnostic["opened_long" if direction == "LONG" else "opened_short"] += 1
                current_positions += 1
                logger.info("PAPER opened %s %s with score %.1f/100", direction, symbol, directional_score)
            else:
                reject("protected_position_creation_rejected", direction)
        logger.info("PAPER entry diagnostics %s", diagnostic)

async def _scan_cycle() -> None:
    async with _cycle_lock:
        try:
            # Bybit Spot only. Groq is a mandatory approval gate for new PAPER entries.
            bybit_payload = await scan_bybit(limit=500, min_volume=0, capital_usdt=_INITIAL_CASH,
                                             risk_pct=_RISK_PCT, fee_rate=_FEE_RATE)
            errors = []
            with _connect() as db:
                open_symbols = [str(row["symbol"]) for row in db.execute(
                    "SELECT symbol FROM paper_positions WHERE exchange='Bybit'").fetchall()]
            try:
                live_quotes = await fetch_spot_ticker_prices(open_symbols)
            except Exception as quote_error:
                live_quotes = {}
                logger.warning("Direct Bybit quotes unavailable for open positions: %s", type(quote_error).__name__)
            if isinstance(bybit_payload, dict):
                assets = bybit_payload.get("assets") or []
                ai_state = get_ai_advisor_state()
                logger.info(
                    "PAPER cycle Bybit assets=%d; AI provider=%s status=%s",
                    len(assets) if isinstance(assets, list) else -1,
                    ai_state.get("provider", "unknown"), ai_state.get("status", "unknown"),
                )
                # Existing-position exits and protections remain active regardless of AI status.
                # New automatic entries require a fresh successful Groq or Gemini recommendation.
                _apply_market_data("Bybit", bybit_payload, allow_new_entries=True,
                                   allow_long_entries=True, allow_short_entries=True,
                                   position_prices=live_quotes,
                                   ai_advisor=ai_state)
            else:
                errors.append("Bybit analysis unavailable; existing positions checked using direct quotes")
                _apply_market_data("Bybit", {"assets": []}, allow_new_entries=False,
                                   allow_short_entries=False, position_prices=live_quotes)
            if isinstance(bybit_payload, dict):
                # Groq reviews existing PAPER positions in shadow mode only; it cannot close them.
                paper_snapshot = _account_snapshot()
                exit_state = await refresh_ai_exit_advisor(
                    paper_snapshot.get("open_positions", []),
                    bybit_payload.get("assets") or [],
                )
                logger.info("PAPER Groq exit review status=%s; recommendations=%d; auto_close=false",
                            exit_state.get("status", "unknown"),
                            len(exit_state.get("recommendations") or []))
            logger.info("Market-data source and PAPER trading venue: Bybit Spot only")
            with _connect() as db:
                db.execute("""UPDATE paper_account SET last_scan_at=?, last_error=?, updated_at=? WHERE id=1""",
                           (_now(), "; ".join(errors) if errors else None, _now()))
        except Exception as exc:
            logger.exception("HAN INVEST paper trading cycle failed")
            with _connect() as db:
                db.execute("UPDATE paper_account SET last_error=?, updated_at=? WHERE id=1",
                           (type(exc).__name__, _now()))

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

@router.post("/buy")
async def buy_paper_position(payload: dict[str, Any] = Body(...)):
    """Manually open a virtual LONG position using Bybit Spot market data."""
    exchange = str(payload.get("exchange") or "").strip()
    symbol = str(payload.get("symbol") or "").strip().upper().replace("-", "").replace("/", "")
    if exchange.lower() != "bybit":
        raise HTTPException(status_code=400, detail="Manual PAPER purchases are restricted to Bybit Spot")
    if not symbol or not symbol.endswith("USDT") or len(symbol) <= 4:
        raise HTTPException(status_code=422, detail="A valid Bybit Spot USDT symbol is required")

    async with _cycle_lock:
        _init_db()
        try:
            market = await scan_bybit(limit=500, min_volume=0, capital_usdt=_INITIAL_CASH,
                                      risk_pct=_RISK_PCT, fee_rate=_FEE_RATE)
        except Exception as exc:
            logger.exception("Manual Bybit PAPER market scan failed")
            raise HTTPException(status_code=503, detail="Bybit Spot market data is temporarily unavailable") from exc

        assets = market.get("assets", []) if isinstance(market, dict) else []
        asset = next((item for item in assets
                      if str(item.get("symbol") or "").upper().replace("-", "").replace("/", "") == symbol), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="This pair is not currently available on Bybit Spot")

        with _connect() as db:
            account = db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
            if account is None or float(account["cash"]) < 10:
                raise HTTPException(status_code=409, detail="Insufficient PAPER balance")
            opened = _open_position(db, "Bybit", asset, "MANUAL_BUY")
            if not opened:
                existing = db.execute(
                    "SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                    ("Bybit", str(asset.get("symbol") or symbol)),
                ).fetchone()
                if existing:
                    raise HTTPException(status_code=409, detail="A Bybit PAPER position for this coin is already open")
                raise HTTPException(status_code=409, detail="Manual buy blocked: valid price, ATR or risk sizing is unavailable")
    logger.info("PAPER manual Bybit Spot buy opened for %s", symbol)
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
