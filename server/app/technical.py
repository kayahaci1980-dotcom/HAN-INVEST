"""Shared, deterministic technical indicators for all HAN INVEST spot scanners.

Inputs must contain completed candles ordered oldest to newest.
RSI and ATR use Wilder smoothing; EMA values are seeded with an SMA.
"""
from __future__ import annotations

from math import isfinite


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _wilder(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("not enough values for Wilder smoothing")
    current = _mean(values[:period])
    for value in values[period:]:
        current = ((current * (period - 1)) + value) / period
    return current


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("not enough values for EMA")
    current = _mean(values[:period])
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def calculate_indicators(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float] | None = None,
    period: int = 14,
) -> dict[str, float] | None:
    """Calculate shared RSI(14), ATR(14), EMA20/EMA50 and volume ratio."""
    if not (len(highs) == len(lows) == len(closes)) or len(closes) < 51:
        return None
    if any(not isfinite(x) or x <= 0 for x in closes[-51:]):
        return None
    if any(not isfinite(x) or x < 0 for x in highs[-51:] + lows[-51:]):
        return None

    gains: list[float] = []
    losses: list[float] = []
    true_ranges: list[float] = []
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
        true_ranges.append(max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        ))

    avg_gain = _wilder(gains, period)
    avg_loss = _wilder(losses, period)
    if avg_loss == 0:
        rsi_value = 100.0 if avg_gain > 0 else 50.0
    else:
        rsi_value = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    atr = _wilder(true_ranges, period)
    price = closes[-1]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    volume_ratio = 0.0
    if volumes is not None and len(volumes) == len(closes) and len(volumes) >= 26:
        baseline = _mean(volumes[-25:-1])
        volume_ratio = volumes[-1] / baseline if baseline > 0 else 0.0

    change24 = (price / closes[-25] - 1.0) * 100.0 if closes[-25] else 0.0
    return {
        "price": price,
        "rsi": rsi_value,
        "rsi_14": rsi_value,
        "atr": atr,
        "atr_14": atr,
        "atr_pct": atr / price * 100.0 if price else 0.0,
        "ema20": ema20,
        "ema50": ema50,
        "volume_ratio": volume_ratio,
        "change24": change24,
        "change_24h_pct": change24,
    }


def score_indicators(data: dict[str, float], quote_volume: float) -> dict[str, float | int]:
    """One 100-point scoring model shared by Binance, Bybit, Coinbase and OKX."""
    price = data["price"]
    ema20 = data["ema20"]
    ema50 = data["ema50"]
    rsi_value = data["rsi_14"]
    atr_pct = data["atr_pct"]
    volume_ratio = data["volume_ratio"]

    trend = 30 if price > ema20 > ema50 else (15 if price > ema20 else 0)
    momentum = 20 if 50 <= rsi_value <= 65 else (10 if 45 <= rsi_value <= 70 else 0)
    volume = 15 if volume_ratio >= 1.5 else (10 if volume_ratio >= 1.2 else 0)
    liquidity = 20 if quote_volume >= 5_000_000 else (
        15 if quote_volume >= 1_000_000 else (5 if quote_volume >= 100_000 else 0)
    )
    volatility = 15 if 0.3 <= atr_pct <= 4.0 else (8 if 0 < atr_pct <= 6.0 else 0)
    score = min(100, trend + momentum + volume + liquidity + volatility)
    return {
        "score": int(score),
        "rsi_14": round(rsi_value, 2),
        "ema20": ema20,
        "ema50": ema50,
        "atr_14": data["atr"],
        "atr_pct": round(atr_pct, 3),
        "volume_ratio": round(volume_ratio, 2),
    }
