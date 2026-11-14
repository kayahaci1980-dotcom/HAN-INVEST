import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.crypto import scan_crypto
from app.bybit import scan_bybit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/paper", tags=["paper-trading"])

# Configure PAPER_DB_PATH to a persistent mounted disk in production.
# Without it, the default /tmp database is intentionally marked non-durable.
_DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/tmp/han-invest-paper.sqlite3"))
_STORAGE_PERSISTENT = bool(os.getenv("PAPER_DB_PATH"))
_INITIAL_CASH = 1000.0
_FEE_RATE = 0.001
_RISK_PCT = 1.0
_MAX_POSITIONS = 3
_SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("PAPER_SCAN_INTERVAL_SECONDS", "300")))
_worker_task: asyncio.Task | None = None
_scan_requested = asyncio.Event()
_cycle_lock = asyncio.Lock()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(_DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db() -> None:
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
        trades = db.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 30"
        ).fetchall()
        # Use the complete ledger for totals; history returned to the app stays capped at 30.
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
        last_price = float(position["last_price"])
        cost = float(position["cost_usdt"])
        market_value = quantity * last_price
        unrealized = market_value - cost
        holdings += market_value
        unrealized_profit += max(0.0, unrealized)
        unrealized_loss += max(0.0, -unrealized)
        open_position_items.append(dict(
            position, side="LONG",
            market_value_usdt=market_value,
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
        "open_positions": open_position_items,
        "history": [dict(t) for t in trades],
        "last_scan_at": account["last_scan_at"],
        "last_error": account["last_error"],
        "storage": "persistent_disk" if _STORAGE_PERSISTENT else "temporary_container_storage",
        "storage_persistent_configured": _STORAGE_PERSISTENT,
        "scan_interval_seconds": _SCAN_INTERVAL_SECONDS,
        "fee_rate_per_side": _FEE_RATE,
        "risk_pct_per_trade": _RISK_PCT,
        "max_open_positions": _MAX_POSITIONS,
        "notice": (
            "Виртуальная Spot-торговля. Реальные ордера на биржи не отправляются. "
            "Для сохранения счёта между перезапусками задайте PAPER_DB_PATH на постоянный диск; "
            "для круглосуточной работы нужен постоянно работающий сервер."
        ),
    }


def _close_position(db: sqlite3.Connection, position: sqlite3.Row,
                    price: float, reason: str) -> None:
    quantity = float(position["quantity"])
    gross = quantity * price
    fee = gross * _FEE_RATE
    net = gross - fee
    pnl = net - float(position["cost_usdt"])
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


def _apply_market_data(exchange: str, payload: dict[str, Any]) -> None:
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        return
    by_symbol = {
        str(asset.get("symbol")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("symbol")
    }
    btc_filter = payload.get("btc_market_filter") or {}
    btc_bullish = bool(btc_filter.get("available")) and (
        btc_filter.get("market_state") == "BULLISH"
    )

    with _connect() as db:
        account = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if not account or not account["enabled"]:
            return

        # Update open positions and close only on confirmed Stop Loss / Take Profit.
        open_positions = db.execute(
            "SELECT * FROM paper_positions WHERE exchange=?", (exchange,)
        ).fetchall()
        for position in open_positions:
            asset = by_symbol.get(position["symbol"])
            if not asset:
                continue  # Missing/stale market data must never trigger a trade.
            price = float(asset.get("price") or 0)
            if price <= 0:
                continue
            db.execute(
                """UPDATE paper_positions SET last_price=?
                   WHERE exchange=? AND symbol=?""",
                (price, exchange, position["symbol"]),
            )
            if price <= float(position["stop_loss"]):
                _close_position(db, position, price, "STOP_LOSS")
            elif price >= float(position["take_profit"]):
                _close_position(db, position, price, "TAKE_PROFIT")

        account = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        current_positions = db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions"
        ).fetchone()["n"]
        cash = float(account["cash"])

        # New entries require an explicit BUY from the scanner and a confirmed bullish BTC filter.
        if not btc_bullish or current_positions >= _MAX_POSITIONS:
            return

        for asset in assets:
            if current_positions >= _MAX_POSITIONS:
                break
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol") or "")
            price = float(asset.get("price") or 0)
            sizing = asset.get("position_sizing") or {}
            if (
                not symbol or price <= 0
                or asset.get("signal") != "BUY"
                or asset.get("trade_ready") is not True
                or int(asset.get("score") or 0) < 80
                or sizing.get("available") is not True
            ):
                continue
            exists = db.execute(
                "SELECT 1 FROM paper_positions WHERE exchange=? AND symbol=?",
                (exchange, symbol),
            ).fetchone()
            if exists:
                continue
            stop = float(asset.get("stop_loss") or 0)
            target = float(asset.get("take_profit") or 0)
            if stop <= 0 or target <= price or stop >= price:
                continue

            desired_qty = float(sizing.get("quantity") or 0)
            affordable_qty = cash / (price * (1 + _FEE_RATE))
            quantity = min(desired_qty, affordable_qty)
            if quantity <= 0:
                continue
            gross = quantity * price
            fee = gross * _FEE_RATE
            total_cost = gross + fee
            if total_cost > cash + 1e-8:
                continue
            db.execute(
                """INSERT INTO paper_positions
                   (exchange, symbol, quantity, cost_usdt, entry_price,
                    stop_loss, take_profit, last_price, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (exchange, symbol, quantity, total_cost, price,
                 stop, target, price, _now()),
            )
            db.execute(
                """UPDATE paper_account SET cash=cash-?, updated_at=? WHERE id=1""",
                (total_cost, _now()),
            )
            db.execute(
                """INSERT INTO paper_trades
                   (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
                   VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, NULL, 'AUTO_SIGNAL')""",
                (_now(), exchange, symbol, price, quantity, gross, fee),
            )
            cash -= total_cost
            current_positions += 1


async def _scan_cycle() -> None:
    async with _cycle_lock:
        try:
            # Each exchange is scanned independently. All requests are public market data only.
            results = await asyncio.gather(
                scan_crypto(limit=50, universe_limit=200, min_volume=1_000_000,
                            capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                            fee_rate=_FEE_RATE),
                scan_bybit(limit=50, min_volume=1_000_000,
                           capital_usdt=_INITIAL_CASH, risk_pct=_RISK_PCT,
                           fee_rate=_FEE_RATE),
                return_exceptions=True,
            )
            errors = []
            successes = 0
            for exchange, payload in zip(("Binance", "Bybit"), results):
                if isinstance(payload, BaseException):
                    errors.append(exchange + ": " + type(payload).__name__)
                    continue
                if not isinstance(payload, dict):
                    errors.append(exchange + ": invalid response")
                    continue
                _apply_market_data(exchange, payload)
                successes += 1
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
                "Автоматический режим не включён: сначала настройте постоянный диск "
                "и переменную PAPER_DB_PATH. Сейчас хранилище временное."
            ),
        )
    _init_db()
    with _connect() as db:
        db.execute(
            "UPDATE paper_account SET enabled=1, updated_at=?, last_error=NULL WHERE id=1",
            (_now(),),
        )
    _scan_requested.set()
    return _account_snapshot()


@router.post("/disable")
async def disable_paper_trading():
    _init_db()
    with _connect() as db:
        db.execute(
            "UPDATE paper_account SET enabled=0, updated_at=? WHERE id=1",
            (_now(),),
        )
    return _account_snapshot()
