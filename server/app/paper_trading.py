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
_RISK_PCT = 1.0
_MAX_POSITIONS = 10
_STOP_LOSS_COOLDOWN_SECONDS = 4 * 60 * 60
_TAKE_PROFIT_COOLDOWN_SECONDS = 1 * 60 * 60
_TRAILING_STOP_COOLDOWN_SECONDS = 2 * 60 * 60
_CONSECUTIVE_LOSSES_COOLDOWN_SECONDS = 8 * 60 * 60
_GLOBAL_COOLDOWN_SYMBOL = "__GLOBAL__"
_SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("PAPER_SCAN_INTERVAL_SECONDS", "60")))
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
        trades = db.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 30"
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

        if direction == "SHORT":
            # Calculate short PnL per lot: add-ons can have different entry prices.
            lots = lots_by_position.get((position["exchange"], position["symbol"]), [])
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
    # Count only fully closed positions. Partial scale-outs are ledger events,
    # not completed trades and must not trigger the global losing-streak pause.
    recent_closes = db.execute(
        """SELECT pnl_usdt FROM paper_trades
           WHERE pnl_usdt IS NOT NULL
             AND reason IN ('STOP_LOSS', 'TAKE_PROFIT', 'TRAILING_STOP', 'MANUAL_CLOSE')
           ORDER BY id DESC LIMIT 2"""
    ).fetchall()
    if len(recent_closes) == 2 and all(float(row["pnl_usdt"]) < 0 for row in recent_closes):
        global_until = _now().timestamp() + _CONSECUTIVE_LOSSES_COOLDOWN_SECONDS
        db.execute(
            """INSERT INTO paper_loss_cooldowns
               (exchange, symbol, blocked_until, loss_pct)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (exchange, symbol) DO UPDATE SET
                   blocked_until=excluded.blocked_until, loss_pct=excluded.loss_pct""",
            ("ALL", _GLOBAL_COOLDOWN_SYMBOL, global_until, loss_pct),
        )
        logger.warning("Two consecutive losing trades: global 8-hour entry pause")
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
            atr_1h = float(asset.get("atr_14_1h") or current["atr_14_1h"] or 0)
            candle_time = str(asset.get("atr_candle_time") or "")
            stop = float(current["stop_loss"] or 0)
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

            previous_candle = str(current["trailing_candle"] or "")
            is_new_candle = bool(candle_time and candle_time != previous_candle)
            # First check the live quote. If it is beyond the stop, simulate a fill
            # at the stop price (or current price when a gap has passed it).
            if stop > 0 and (price <= stop if direction == "LONG" else price >= stop):
                stop_reason = "TRAILING_STOP" if int(current["trailing_stage"] or 0) > 0 else "STOP_LOSS"
                _close_position(db, current, min(price, stop) if direction == "LONG" else max(price, stop), stop_reason)
                continue

            # Also inspect the just-completed 1H candle for a stop wick that may
            # have crossed and recovered between 60-second quote scans.
            candle_low = float(asset.get("candle_low_1h") or 0)
            candle_high = float(asset.get("candle_high_1h") or 0)
            crossed_in_candle = (
                candle_low > 0 and candle_low <= stop if direction == "LONG" and stop > 0
                else candle_high >= stop if direction == "SHORT" and stop > 0
                else False
            )
            if is_new_candle and crossed_in_candle:
                stop_reason = "TRAILING_STOP" if int(current["trailing_stage"] or 0) > 0 else "STOP_LOSS"
                _close_position(db, current, stop, stop_reason)
                continue

            # Track the favorable extreme of the latest completed 1H candle.
            extreme = float(current["peak_price"] or entry)
            candle_extreme = float(asset.get("candle_high_1h" if direction == "LONG" else "candle_low_1h") or price)
            extreme = max(extreme, candle_extreme) if direction == "LONG" else min(extreme, candle_extreme)
            db.execute("UPDATE paper_positions SET peak_price=? WHERE exchange=? AND symbol=?",
                       (extreme, exchange, position["symbol"]))
            # Recalculate only when a new completed 1H candle is available.
            if initial_risk > 0 and atr_1h > 0 and candle_time and candle_time != previous_candle:
                favorable = (extreme - entry) if direction == "LONG" else (entry - extreme)
                r_multiple = max(0.0, favorable / initial_risk)
                stage = int(current["trailing_stage"] or 0)
                candidate, new_stage = stop, stage
                if r_multiple >= 3.0:
                    candidate = extreme - 1.5 * atr_1h if direction == "LONG" else extreme + 1.5 * atr_1h
                    new_stage = 3
                elif r_multiple >= 2.0:
                    candidate = extreme - 2.0 * atr_1h if direction == "LONG" else extreme + 2.0 * atr_1h
                    new_stage = max(stage, 2)
                elif r_multiple >= 1.5:
                    candidate, new_stage = entry, max(stage, 1)
                tightened = max(stop, candidate) if direction == "LONG" else min(stop, candidate)
                db.execute("""UPDATE paper_positions SET stop_loss=?, peak_price=?, atr_14_1h=?,
                    trailing_stage=?, trailing_candle=? WHERE exchange=? AND symbol=?""",
                    (tightened, extreme, atr_1h, new_stage, candle_time, exchange, position["symbol"]))
            if direction == "LONG":
                _manage_long_scale(db, current, price)
            else:
                _manage_short_scale(db, current, price)

        # Market breadth blocks only new entries. Position exits above always run.
        if not allow_new_entries and not allow_short_entries:
            return

        global_cooldown = db.execute(
            "SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?",
            ("ALL", _GLOBAL_COOLDOWN_SYMBOL),
        ).fetchone()
        if global_cooldown and float(global_cooldown["blocked_until"]) > _now().timestamp():
            logger.info("New entries paused globally after two consecutive losses")
            return
        current_positions = db.execute("SELECT COUNT(*) AS n FROM paper_positions").fetchone()["n"]
        if current_positions >= _MAX_POSITIONS:
            return
        candidates = sorted(
            (x for x in assets if isinstance(x, dict)),
            key=lambda x: float(x.get("quote_volume_24h") or 0), reverse=True,
        )
        for asset in candidates:
            if current_positions >= _MAX_POSITIONS:
                break
            try:
                price = float(asset.get("price") or 0)
                atr = float(asset.get("atr_14_1h") or 0)
                ma20 = float(asset.get("ma20") or asset.get("ema20") or 0)
                ma50 = float(asset.get("ma50") or 0)
                daily_open = float(asset.get("daily_open") or 0)
                volume = float(asset.get("quote_volume_24h") or 0)
            except (TypeError, ValueError):
                continue
            if (asset.get("trade_ready") is not True or not asset.get("symbol")
                    or _is_stablecoin_pair(str(asset.get("symbol")))
                    or price <= 0 or atr <= 0 or daily_open <= 0
                    or volume <= 50_000_000 or str(asset.get("atr_timeframe") or "") != "1H"
                    or not asset.get("atr_candle_time")):
                continue
            cooldown = db.execute("SELECT blocked_until FROM paper_loss_cooldowns WHERE exchange=? AND symbol=?",
                                  (exchange, str(asset["symbol"]))).fetchone()
            if cooldown and float(cooldown["blocked_until"]) > _now().timestamp():
                continue
            opened = False
            if (allow_new_entries and ma20 > 0 and ma50 > 0 and price > ma20 > ma50
                    and price < daily_open + 0.5 * atr):
                opened = _open_position(db, exchange, asset, "AUTO_LONG_ATR_PRO_RR_1_2")
            elif (allow_short_entries and ma20 > 0 and ma50 > 0 and price < ma20 < ma50
                    and price > daily_open - 0.5 * atr):
                opened = _open_short_position(db, exchange, asset, "AUTO_SHORT_ATR_PRO_RR_1_2")
            if opened:
                current_positions += 1


async def _scan_cycle() -> None:
    global _last_market_breadth
    async with _cycle_lock:
        try:
            # Each exchange is scanned independently. All requests are public market data only.
            gathered = await asyncio.gather(
                get_market_breadth(),
                scan_crypto(limit=50, universe_limit=200, min_volume=50_000_001,
                            capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                            fee_rate=_FEE_RATE),
                scan_bybit(limit=50, min_volume=50_000_001,
                           capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                           fee_rate=_FEE_RATE),
                scan_coinbase(limit=50, min_volume=50_000_001),
                scan_okx(limit=50, min_volume=50_000_001),
                return_exceptions=True,
            )
            breadth_result, *results = gathered
            errors = []
            if isinstance(breadth_result, BaseException) or not isinstance(breadth_result, dict):
                # Fail open only for the optional global breadth overlay:
                # Bybit's own per-symbol trend, volume, ATR, RR and daily-open
                # filters still decide whether a LONG or SHORT may be opened.
                # Position management and protective exits run independently above.
                allow_new_entries = True
                allow_short_entries = True
                _last_market_breadth = {
                    "available": False, "status": "UNAVAILABLE",
                    "allow_new_entries": True,
                    "allow_short_entries": True,
                    "fallback": "BYBIT_ONLY",
                    "reason": "Глобальные данные недоступны; входы проверяются по локальным фильтрам Bybit.",
                }
                errors.append("Market breadth: unavailable; using Bybit-only entry filters")
                logger.warning("Market breadth unavailable; falling back to Bybit-only entry filters")
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
            # Binance, Coinbase Exchange and OKX are analysis-only feeds.
            # Never pass their payloads to the PAPER position manager.
            logger.info("Paper trading venue: Bybit Spot only; Binance/Coinbase/OKX analysis-only")
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
