import asyncio
import math
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)
COINBASE = "https://api.exchange.coinbase.com"
OKX = "https://www.okx.com"
CACHE_SECONDS = 45
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _score_asset(closes: list[float], highs: list[float], lows: list[float],
                 volumes: list[float], price: float, quote_volume: float) -> dict[str, float]:
    ma20 = _mean(closes[-20:])
    ma50 = _mean(closes[-50:])
    change6 = (closes[-1] / closes[-7] - 1) * 100 if closes[-7] else 0.0
    change24 = (closes[-1] / closes[-25] - 1) * 100 if closes[-25] else 0.0
    returns = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes)) if closes[i-1] > 0]
    volatility = math.sqrt(_mean([x*x for x in returns])) * 100
    baseline = _mean(volumes[-25:-1])
    volume_ratio = volumes[-1] / baseline if baseline > 0 else 0.0
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, len(closes))
    ]
    trend = 20 if closes[-1] > ma20 > ma50 else 13 if closes[-1] > ma20 else 5
    momentum = max(0, min(20, round(10 + change24 * 0.8)))
    short = max(0, min(20, round(10 + change6 * 1.2)))
    liquidity = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
    stability = max(0, min(20, round(20 - volatility * 2)))
    volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
    return {
        "score": float(min(100, trend + momentum + short + liquidity + stability + volume_score)),
        "ma20": ma20, "ma50": ma50, "momentum_6h_pct": change6,
        "momentum_24h_pct": change24, "hourly_volatility_pct": volatility,
        "volume_ratio": volume_ratio, "atr_14": _mean(true_ranges[-14:]),
    }


async def _coinbase_scan(limit: int, min_volume: float) -> dict[str, Any]:
    timeout = httpx.Timeout(20.0)
    headers = {"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        products_response = await client.get(f"{COINBASE}/products")
        products_response.raise_for_status()
        products = [
            p for p in products_response.json()
            if isinstance(p, dict)
            and p.get("quote_currency") == "USDT"
            and p.get("status") == "online"
            and not p.get("trading_disabled", False)
            and not p.get("cancel_only", False)
            and not p.get("post_only", False)
            and not p.get("limit_only", False)
        ]
        semaphore = asyncio.Semaphore(5)

        async def analyze(product: dict[str, Any]):
            async with semaphore:
                product_id = str(product.get("id") or "")
                if not product_id:
                    return None
                try:
                    stats_response = await client.get(f"{COINBASE}/products/{product_id}/stats")
                    stats_response.raise_for_status()
                    stats = stats_response.json()
                    price = float(stats.get("last") or 0)
                    base_volume = float(stats.get("volume") or 0)
                    quote_volume = price * base_volume
                    if price <= 0 or quote_volume < min_volume:
                        return None
                    end = datetime.now(timezone.utc)
                    start_hour = (end - timedelta(hours=62)).isoformat()
                    end_iso = end.isoformat()
                    start_day = (end - timedelta(days=20)).isoformat()
                    hourly_response, daily_response = await asyncio.gather(
                        client.get(f"{COINBASE}/products/{product_id}/candles",
                                   params={"granularity": 3600, "start": start_hour, "end": end_iso}),
                        client.get(f"{COINBASE}/products/{product_id}/candles",
                                   params={"granularity": 86400, "start": start_day, "end": end_iso}),
                    )
                    hourly_response.raise_for_status()
                    daily_response.raise_for_status()
                    hourly = sorted(hourly_response.json(), key=lambda x: x[0])[:-1]
                    daily = sorted(daily_response.json(), key=lambda x: x[0])[:-1]
                    if len(hourly) < 51 or len(daily) < 15:
                        return None
                    closes = [float(x[4]) for x in hourly]
                    lows = [float(x[1]) for x in hourly]
                    highs = [float(x[2]) for x in hourly]
                    volumes = [float(x[5]) for x in hourly]
                    daily_closes = [float(x[4]) for x in daily]
                    daily_highs = [float(x[2]) for x in daily]
                    daily_lows = [float(x[1]) for x in daily]
                    daily_ranges = [
                        max(daily_highs[i] - daily_lows[i],
                            abs(daily_highs[i] - daily_closes[i-1]),
                            abs(daily_lows[i] - daily_closes[i-1]))
                        for i in range(1, len(daily_closes))
                    ]
                    metrics = _score_asset(closes, highs, lows, volumes, price, quote_volume)
                    atr = _mean(daily_ranges[-14:])
                    if not math.isfinite(atr) or atr <= 0:
                        return None
                    return {
                        "symbol": product_id, "name": product.get("base_currency", product_id),
                        "exchange": "Coinbase Exchange", "quote_currency": "USDT",
                        "price": price, "quote_volume_24h": round(quote_volume, 2),
                        "change_24h_pct": round((price / float(stats.get("open") or price) - 1) * 100, 2),
                        "score": int(metrics["score"]), "signal": "WATCH",
                        "trade_ready": True, "exchange_pair_verified": True,
                        "pair_status": "online", "atr_14": atr, "atr_14_1d": atr,
                        "atr_timeframe": "1D", "ma20": metrics["ma20"], "ma50": metrics["ma50"],
                        "momentum_6h_pct": metrics["momentum_6h_pct"],
                        "momentum_24h_pct": metrics["momentum_24h_pct"],
                        "hourly_volatility_pct": metrics["hourly_volatility_pct"],
                        "volume_ratio": metrics["volume_ratio"],
                    }
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                    logger.warning("Coinbase analysis skipped for %s: %s", product_id, type(exc).__name__)
                    return None

        rows = await asyncio.gather(*(analyze(p) for p in products))
        assets = [x for x in rows if x]
        assets.sort(key=lambda x: x["score"], reverse=True)
        for rank, asset in enumerate(assets, 1):
            asset["market_rank"] = rank
        return {
            "source": "Coinbase Exchange public market data",
            "exchange": "Coinbase Exchange", "scanned": min(len(assets), limit),
            "eligible_pairs": len(products),
            "diagnostics": {
                "exchange_pairs": len(products),
                "analysis_attempted": len(products),
                "analysis_succeeded": len(assets),
                "analysis_skipped": len(products) - len(assets),
                "skip_reason": "Пара не прошла фильтр объёма либо свечи/API/данные не прошли проверку; детали в логах сервера.",
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "assets": assets[:limit],
        }


async def _okx_scan(limit: int, min_volume: float) -> dict[str, Any]:
    timeout = httpx.Timeout(20.0)
    headers = {"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        instruments_response, tickers_response = await asyncio.gather(
            client.get(f"{OKX}/api/v5/public/instruments", params={"instType": "SPOT"}),
            client.get(f"{OKX}/api/v5/market/tickers", params={"instType": "SPOT"}),
        )
        instruments_response.raise_for_status()
        tickers_response.raise_for_status()
        instruments_data = instruments_response.json()
        tickers_data = tickers_response.json()
        if instruments_data.get("code") != "0" or tickers_data.get("code") != "0":
            raise ValueError("OKX returned an API error")
        instruments = {
            x.get("instId"): x for x in instruments_data.get("data", [])
            if x.get("quoteCcy") == "USDT" and x.get("state") == "live"
        }
        tickers = [
            x for x in tickers_data.get("data", [])
            if x.get("instId") in instruments
            and float(x.get("volCcy24h") or 0) >= min_volume
        ]
        tickers.sort(key=lambda x: float(x.get("volCcy24h") or 0), reverse=True)
        semaphore = asyncio.Semaphore(5)

        async def analyze(ticker: dict[str, Any]):
            async with semaphore:
                inst_id = str(ticker.get("instId") or "")
                price = float(ticker.get("last") or 0)
                quote_volume = float(ticker.get("volCcy24h") or 0)
                if not inst_id or price <= 0:
                    return None
                try:
                    hourly_response, daily_response = await asyncio.gather(
                        client.get(f"{OKX}/api/v5/market/candles",
                                   params={"instId": inst_id, "bar": "1H", "limit": "60"}),
                        client.get(f"{OKX}/api/v5/market/candles",
                                   params={"instId": inst_id, "bar": "1D", "limit": "20"}),
                    )
                    hourly_response.raise_for_status()
                    daily_response.raise_for_status()
                    hdata, ddata = hourly_response.json(), daily_response.json()
                    if hdata.get("code") != "0" or ddata.get("code") != "0":
                        return None
                    hourly = list(reversed(hdata.get("data", [])))
                    daily = list(reversed(ddata.get("data", [])))
                    hourly = [x for x in hourly if len(x) > 8 and x[8] == "1"][-60:]
                    daily = [x for x in daily if len(x) > 8 and x[8] == "1"][-20:]
                    if len(hourly) < 51 or len(daily) < 15:
                        return None
                    closes = [float(x[4]) for x in hourly]
                    highs = [float(x[2]) for x in hourly]
                    lows = [float(x[3]) for x in hourly]
                    volumes = [float(x[7]) for x in hourly]
                    daily_closes = [float(x[4]) for x in daily]
                    daily_highs = [float(x[2]) for x in daily]
                    daily_lows = [float(x[3]) for x in daily]
                    daily_ranges = [
                        max(daily_highs[i] - daily_lows[i],
                            abs(daily_highs[i] - daily_closes[i-1]),
                            abs(daily_lows[i] - daily_closes[i-1]))
                        for i in range(1, len(daily_closes))
                    ]
                    metrics = _score_asset(closes, highs, lows, volumes, price, quote_volume)
                    atr = _mean(daily_ranges[-14:])
                    if not math.isfinite(atr) or atr <= 0:
                        return None
                    open24 = float(ticker.get("open24h") or price)
                    return {
                        "symbol": inst_id, "name": inst_id.split("-")[0],
                        "exchange": "OKX", "quote_currency": "USDT",
                        "price": price, "quote_volume_24h": round(quote_volume, 2),
                        "change_24h_pct": round((price / open24 - 1) * 100, 2) if open24 else 0,
                        "score": int(metrics["score"]), "signal": "WATCH",
                        "trade_ready": True, "exchange_pair_verified": True,
                        "pair_status": "live", "atr_14": atr, "atr_14_1d": atr,
                        "atr_timeframe": "1D", "ma20": metrics["ma20"], "ma50": metrics["ma50"],
                        "momentum_6h_pct": metrics["momentum_6h_pct"],
                        "momentum_24h_pct": metrics["momentum_24h_pct"],
                        "hourly_volatility_pct": metrics["hourly_volatility_pct"],
                        "volume_ratio": metrics["volume_ratio"],
                    }
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                    logger.warning("OKX analysis skipped for %s: %s", inst_id, type(exc).__name__)
                    return None

        rows = await asyncio.gather(*(analyze(t) for t in tickers[:limit]))
        assets = [x for x in rows if x]
        assets.sort(key=lambda x: x["score"], reverse=True)
        for rank, asset in enumerate(assets, 1):
            asset["market_rank"] = rank
        return {
            "source": "OKX public market data",
            "exchange": "OKX", "scanned": len(assets),
            "eligible_pairs": len(instruments),
            "volume_qualified_candidates": len(tickers),
            "diagnostics": {
                "exchange_pairs": len(instruments),
                "volume_qualified_candidates": len(tickers),
                "analysis_attempted": min(len(tickers), limit),
                "analysis_succeeded": len(assets),
                "analysis_skipped": min(len(tickers), limit) - len(assets),
                "skip_reason": "Свечи/API/данные не прошли проверку; детали в логах сервера.",
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "assets": assets,
        }


async def _cached_scan(exchange: str, limit: int, min_volume: float):
    import time
    key = f"{exchange}:{limit}:{min_volume}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        result = await (_coinbase_scan(limit, min_volume) if exchange == "coinbase"
                       else _okx_scan(limit, min_volume))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"{exchange.upper()} market data temporarily unavailable") from exc
    _cache[key] = (time.time(), result)
    return result


@router.get("/api/v1/crypto/coinbase/scan")
async def scan_coinbase(
    limit: int = Query(50, ge=1, le=500),
    min_volume: float = Query(1_000_000, ge=0),
):
    return await _cached_scan("coinbase", limit, min_volume)


@router.get("/api/v1/crypto/okx/scan")
async def scan_okx(
    limit: int = Query(50, ge=1, le=500),
    min_volume: float = Query(1_000_000, ge=0),
):
    return await _cached_scan("okx", limit, min_volume)
