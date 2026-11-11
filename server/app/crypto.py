import asyncio
import math
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)
# Binance documents this host specifically for public market-data-only requests.
BASE_URL = "https://data-api.binance.vision"
CACHE_TTL = 60
_cache: dict[str, tuple[float, Any]] = {}
_semaphore = asyncio.Semaphore(5)

EXCLUDED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _score_change(value: float, scale: float = 5.0) -> int:
    return max(0, min(20, round(10 + value * scale)))


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None):
    async with _semaphore:
        response = await client.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response.json()


async def _analyze_symbol(client: httpx.AsyncClient, ticker: dict):
    symbol = ticker.get("symbol", "")
    try:
        candles = await _get_json(
            client,
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1h", "limit": 60},
        )
        closes = [float(row[4]) for row in candles if len(row) > 4]
        volumes = [float(row[5]) for row in candles if len(row) > 5]
        if len(closes) < 50:
            return None

        last = closes[-1]
        ma20 = _mean(closes[-20:])
        ma50 = _mean(closes[-50:])
        change_6h = ((last / closes[-7]) - 1) * 100 if closes[-7] else 0
        change_24h = ((last / closes[-25]) - 1) * 100 if closes[-25] else 0
        returns = [
            (closes[i] / closes[i - 1] - 1)
            for i in range(1, len(closes)) if closes[i - 1]
        ]
        hourly_volatility = math.sqrt(_mean([x * x for x in returns])) * 100
        quote_volume = float(ticker.get("quoteVolume", 0))
        volume_ratio = (
            volumes[-1] / _mean(volumes[-25:-1])
            if len(volumes) >= 25 and _mean(volumes[-25:-1]) > 0 else 1.0
        )

        trend_score = 20 if last > ma20 > ma50 else 13 if last > ma20 else 5
        momentum_score = _score_change(change_24h, 0.8)
        short_score = _score_change(change_6h, 1.2)
        liquidity_score = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
        stability_score = max(0, min(20, round(20 - hourly_volatility * 2)))
        volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
        score = min(100, trend_score + momentum_score + short_score +
                    liquidity_score + stability_score + volume_score)

        if score >= 75 and last > ma20 and change_6h > 0:
            signal = "BUY"
        elif score >= 55:
            signal = "WATCH"
        else:
            signal = "HOLD"

        risk = "Высокий" if hourly_volatility >= 2.5 else (
            "Средний" if hourly_volatility >= 1.2 else "Умеренный"
        )

        return {
            "symbol": symbol,
            "price": last,
            "change_24h_pct": round(float(ticker.get("priceChangePercent", 0)), 2),
            "momentum_6h_pct": round(change_6h, 2),
            "momentum_24h_pct": round(change_24h, 2),
            "quote_volume_24h": round(quote_volume, 2),
            "volume_ratio": round(volume_ratio, 2),
            "ma20": round(ma20, 8),
            "ma50": round(ma50, 8),
            "hourly_volatility_pct": round(hourly_volatility, 3),
            "score": score,
            "signal": signal,
            "risk": risk,
        }
    except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


@router.get("/api/v1/crypto/scan")
async def scan_crypto(
    limit: int = Query(30, ge=5, le=50),
    min_volume: float = Query(1_000_000, ge=0),
):
    cache_key = f"crypto:{limit}:{min_volume}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            tickers = await _get_json(client, "/api/v3/ticker/24hr")
            if not isinstance(tickers, list):
                raise HTTPException(status_code=503, detail="Binance вернула неожиданный формат данных.")

            eligible = [
                item for item in tickers
                if isinstance(item, dict)
                and item.get("symbol", "").endswith("USDT")
                and not item.get("symbol", "").endswith(EXCLUDED_SUFFIXES)
                and float(item.get("quoteVolume", 0) or 0) >= min_volume
            ]
            eligible.sort(key=lambda item: float(item.get("quoteVolume", 0)), reverse=True)
            selected = eligible[:limit]
            results = await asyncio.gather(
                *[_analyze_symbol(client, item) for item in selected]
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.exception("Binance market-data request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Криптовалютные данные Binance временно недоступны.",
        ) from exc

    assets = [item for item in results if item is not None]
    assets.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "source": "Binance public market data",
        "scanned": len(assets),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets,
        "notice": (
            "Оценка является аналитическим индикатором, а не гарантией прибыли. "
            "Не учитывает комиссии, проскальзывание и индивидуальные ограничения."
        ),
    }
    _cache[cache_key] = (time.time(), payload)
    return payload
