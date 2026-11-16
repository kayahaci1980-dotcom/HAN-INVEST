#!/usr/bin/env python3
"""Research-only Bybit Spot historical filter comparison.

This script does not import or modify the live PAPER engine. It uses completed
1h candles, enters at the next candle open, applies 0.1% fees per side and a
fixed -1 USDT net-loss stop for a 100 USDT position. If stop and target logic
would be ambiguous inside one candle, the stop is assumed to execute first.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BASE = "https://api.bybit.com"
DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT", "BCHUSDT",
)
FEE = 0.001
TICKET = 100.0
STOP_NET = 1.0
MAX_HOLD_HOURS = 24
ATR_FLOORS = (0.0, 0.0025, 0.005, 0.01)


@dataclass
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


async def fetch_klines(client: httpx.AsyncClient, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
    rows: dict[int, Candle] = {}
    cursor = start_ms
    while cursor < end_ms:
        response = await client.get(
            f"{BASE}/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": "60",
                    "start": cursor, "end": end_ms, "limit": 1000},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit {symbol}: {payload.get('retMsg')}")
        batch = payload.get("result", {}).get("list", [])
        if not batch:
            break
        for row in batch:
            candle = Candle(int(row[0]), *(_number(x) for x in row[1:7]))
            rows[candle.start] = candle
        newest = max(int(row[0]) for row in batch)
        next_cursor = newest + 60 * 60 * 1000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.05)
    return [rows[key] for key in sorted(rows) if start_ms <= key < end_ms]


def sma(values: list[float], period: int) -> float:
    return sum(values[-period:]) / period if len(values) >= period else 0.0


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for item in values[period:]:
        value = item * alpha + value * (1.0 - alpha)
    return value


def atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1 or candles[-1].close <= 0:
        return 0.0
    ranges = []
    for prev, cur in zip(candles[-period-1:-1], candles[-period:]):
        ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return (sum(ranges) / len(ranges)) / candles[-1].close


def breadth_at(history: dict[str, list[Candle]], index: int) -> tuple[bool, bool]:
    rising = falling = measured = 0
    for candles in history.values():
        if index >= 24 and index < len(candles):
            old, new = candles[index - 24].close, candles[index].close
            if old > 0 and new > 0:
                measured += 1
                rising += new > old
                falling += new < old
    if not measured:
        return False, False
    return rising / measured >= 0.5, falling / measured >= 0.5


def simulate_variant(history: dict[str, list[Candle]], direction_mode: str, atr_floor: float) -> dict[str, Any]:
    # This research model allows one open position per symbol and caps the
    # portfolio at ten. Each trade uses a fixed 100 USDT notional.
    positions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    common_length = min(map(len, history.values()))
    for i in range(60, common_length - 1):
        allow_long, allow_short = breadth_at(history, i)
        for symbol, candles in history.items():
            candle = candles[i]
            nxt = candles[i + 1]
            pos = positions.get(symbol)
            if pos:
                exit_price = None
                reason = None
                if pos["side"] == "LONG" and candle.low <= pos["stop"]:
                    exit_price, reason = pos["stop"], "STOP_LOSS"
                elif pos["side"] == "SHORT" and candle.high >= pos["stop"]:
                    exit_price, reason = pos["stop"], "STOP_LOSS"
                elif i - pos["entry_index"] >= MAX_HOLD_HOURS:
                    exit_price, reason = candle.close, "TIME_EXIT"
                if exit_price is not None:
                    qty = pos["qty"]
                    gross = (exit_price - pos["entry"]) * qty * (1 if pos["side"] == "LONG" else -1)
                    fees = (pos["entry"] * qty + exit_price * qty) * FEE
                    trades.append({"symbol": symbol, "side": pos["side"], "net": gross - fees, "reason": reason})
                    del positions[symbol]
                continue
            if len(positions) >= 10 or symbol in positions:
                continue
            window = candles[:i + 1]
            closes = [x.close for x in window]
            current = candle.close
            ma20 = sma(closes, 20)
            ma50 = sma(closes, 50)
            ema20 = ema(closes, 20)
            vol_base = sum(x.volume for x in window[-25:-1]) / 24
            vol_ratio = candle.volume / vol_base if vol_base > 0 else 0
            a_pct = atr_pct(window)
            if not (current > 0 and candle.turnover >= 3_000_000 and a_pct >= atr_floor and vol_ratio >= 0.8):
                continue
            long_ok = allow_long and direction_mode == "FLEX" and current > ema20
            short_ok = allow_short and direction_mode == "FLEX" and current < ema20
            if direction_mode == "STRICT":
                long_ok = allow_long and current > ma20 > ma50 and vol_ratio >= 1.2
                short_ok = allow_short and current < ma20 < ma50 and vol_ratio >= 1.2
            if not (long_ok or short_ok):
                continue
            side = "LONG" if long_ok else "SHORT"
            entry = nxt.open
            if entry <= 0:
                continue
            qty = TICKET / entry
            # Solve for a stop whose estimated round-trip net loss is $1.
            if side == "LONG":
                stop = (TICKET - STOP_NET) / (qty * (1 - FEE))
            else:
                stop = (TICKET + STOP_NET) / (qty * (1 + FEE))
            positions[symbol] = {"side": side, "entry": entry, "qty": qty,
                                 "stop": stop, "entry_index": i + 1}
    net_values = [x["net"] for x in trades]
    equity = peak = 1000.0
    max_dd = 0.0
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    wins = sum(x > 0 for x in net_values)
    return {
        "direction_mode": direction_mode,
        "atr_floor_pct": atr_floor * 100,
        "trades": len(trades),
        "net_pnl_usdt": round(sum(net_values), 4),
        "win_rate_pct": round(100 * wins / len(trades), 2) if trades else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "stop_loss_count": sum(x["reason"] == "STOP_LOSS" for x in trades),
        "method_note": "hourly OHLC approximation; intrabar stop assumed first",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default="bybit_historical_filter_report.csv")
    args = parser.parse_args()
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=max(30, min(args.days, 180)))
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "HAN-INVEST-research/1.0"}) as client:
        fetched = await asyncio.gather(*(fetch_klines(client, s, start_ms, end_ms) for s in symbols),
                                       return_exceptions=True)
    history = {s: rows for s, rows in zip(symbols, fetched)
               if isinstance(rows, list) and len(rows) >= 100}
    if len(history) < 3:
        raise RuntimeError("Недостаточно исторических данных Bybit Spot: доступно меньше 3 пар")
    # Align series by common timestamps to avoid comparing different market hours.
    common = set.intersection(*(set(c.start for c in rows) for rows in history.values()))
    if len(common) < 100:
        raise RuntimeError("Недостаточно общих часовых свечей для корректного сравнения")
    aligned = {s: [next(c for c in rows if c.start == ts) for ts in sorted(common)]
               for s, rows in history.items()}
    report = [simulate_variant(aligned, mode, floor)
              for mode in ("STRICT", "FLEX") for floor in ATR_FLOORS]
    output = Path(args.output)
    with output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(report[0]))
        writer.writeheader()
        writer.writerows(report)
    print(f"Период UTC: {start.isoformat()} — {end.isoformat()}")
    print(f"Пары с достаточными данными: {', '.join(aligned)}")
    for row in report:
        print(row)
    print(f"CSV: {output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
