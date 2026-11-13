"""Public spot-market scanners for Coinbase Exchange and OKX.

Read-only market data only. This module never places or signs trading orders.
"""
import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.technical import calculate_indicators, score_indicators

router = APIRouter()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL = 45


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values: list[float], period: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes[-period:]]
    losses = [max(-x, 0.0) for x in changes[-period:]]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def indicators(rows: list[list[Any]], venue: str) -> dict[str, float] | None:
    # Both providers return newest-first candles. Drop the forming candle,
    # then pass only completed candles in chronological order.
    ordered = list(reversed(rows))[:-1] if rows else []
    try:
        if venue == "Coinbase":
            highs = [float(x[2]) for x in ordered]
            lows = [float(x[1]) for x in ordered]
            closes = [float(x[4]) for x in ordered]
            volumes = [float(x[5]) for x in ordered]
        else:
            highs = [float(x[2]) for x in ordered]
            lows = [float(x[3]) for x in ordered]
            closes = [float(x[4]) for x in ordered]
            volumes = [float(x[5]) for x in ordered]
        return calculate_indicators(highs, lows, closes, volumes)
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return None

def score_asset(data: dict[str, float], quote_volume: float) -> dict[str, Any]:
    return score_indicators(data, quote_volume)


async def fetch_json(client: httpx.AsyncClient, url: str, params: dict | None = None):
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def scan_coinbase(limit: int, min_volume: float):
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
        products = await fetch_json(client, "https://api.exchange.coinbase.com/products")
        if not isinstance(products, list):
            raise HTTPException(status_code=503, detail="Coinbase вернула неверный список рынков.")
        eligible = [
            p for p in products if isinstance(p, dict)
            and p.get("quote_currency") in ("USD", "USDT")
            and p.get("status") == "online"
            and not p.get("trading_disabled", False)
            and p.get("id") and p.get("base_currency") not in {
                "USDT", "USDC", "DAI", "EUR", "GBP"
            }
        ]
        sem = asyncio.Semaphore(5)

        async def analyze(product):
            async with sem:
                product_id = product["id"]
                try:
                    ticker, candles = await asyncio.gather(
                        fetch_json(client, f"https://api.exchange.coinbase.com/products/{product_id}/ticker"),
                        fetch_json(client, f"https://api.exchange.coinbase.com/products/{product_id}/candles",
                                   {"granularity": 3600, "limit": 60}),
                    )
                    rows = candles if isinstance(candles, list) else []
                    data = indicators(rows, "Coinbase")
                    if not data:
                        return None
                    price = float(ticker.get("price") or data["price"])
                    base_volume = float(ticker.get("volume") or 0)
                    quote_volume = base_volume * price
                    if quote_volume < min_volume:
                        return None
                    data["price"] = price
                    scored = score_asset(data, quote_volume)
                    base, quote = product["base_currency"], product["quote_currency"]
                    symbol = base + quote
                    btc_allowed = btc_state == "BULLISH"
                    confirmed = (
                        scored["score"] >= 83 and price > data["ema20"] > data["ema50"]
                        and data["change24"] >= 1.0 and data["volume_ratio"] >= 1.2
                        and quote_volume >= min_volume
                    )
                    stop = price - 2.0 * data["atr"]
                    target = price + 4.0 * data["atr"]
                    return {
                        "symbol": symbol, "name": base, "exchange": "Coinbase Exchange",
                        "source": "coinbase", "quote_currency": quote, "price": price,
                        "change_24h_pct": round(data["change24"], 2),
                        "quote_volume_24h": round(quote_volume, 2),
                        "trade_ready": True, "exchange_pair_verified": True,
                        "volume_confirmed": data["volume_ratio"] >= 1.2,
                        "btc_buy_allowed": btc_allowed,
                        "signal": "BUY" if confirmed and btc_allowed else "HOLD",
                        "early_signal": "EARLY BUY" if scored["score"] >= 75 and not (confirmed and btc_allowed) else None,
                        "stop_loss": round(stop, 10), "take_profit": round(target, 10),
                        **scored,
                    }
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    return None

        btc_product = next((p for p in eligible if p.get("id") == "BTC-USD"), None)
        btc_state = "UNKNOWN"
        if btc_product:
            try:
                rows = await fetch_json(client, "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                                        {"granularity": 3600, "limit": 60})
                btc = indicators(rows, "Coinbase")
                if btc:
                    btc_state = "BULLISH" if btc["price"] > btc["ema20"] > btc["ema50"] else "BEARISH"
                    if btc_state != "BULLISH" and btc["price"] > btc["ema20"]:
                        btc_state = "SIDEWAYS"
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        chosen = eligible[:max(limit * 3, 60)]
        results = await asyncio.gather(*(analyze(p) for p in chosen))
        assets = sorted([x for x in results if x], key=lambda x: x["score"], reverse=True)[:limit]
        return {
            "source": "Coinbase Exchange public spot data", "exchange": "Coinbase Exchange",
            "scanned": len(assets), "btc_market_filter": {
                "available": btc_state != "UNKNOWN", "buy_allowed": btc_state == "BULLISH",
                "market_state": btc_state
            }, "updated_at": datetime.now(timezone.utc).isoformat(), "assets": assets,
            "notice": "Только публичные спотовые рынки Coinbase. Сделки в приложении — PAPER, реальные ордера не отправляются."
        }


async def scan_okx(limit: int, min_volume: float):
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
        instruments_data, tickers_data = await asyncio.gather(
            fetch_json(client, "https://www.okx.com/api/v5/public/instruments", {"instType": "SPOT"}),
            fetch_json(client, "https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"}),
        )
        instruments = instruments_data.get("data", []) if isinstance(instruments_data, dict) else []
        tickers = tickers_data.get("data", []) if isinstance(tickers_data, dict) else []
        active = {p.get("instId"): p for p in instruments if p.get("state") == "live"
                  and p.get("quoteCcy") == "USDT" and p.get("instId")}
        eligible = []
        for ticker in tickers:
            inst = ticker.get("instId")
            if inst not in active:
                continue
            price = float(ticker.get("last") or 0)
            quote_volume = float(ticker.get("volCcy24h") or 0)
            if price > 0 and quote_volume >= min_volume:
                eligible.append((ticker, active[inst], quote_volume))
        eligible.sort(key=lambda x: x[2], reverse=True)
        btc_state = "UNKNOWN"
        try:
            rows = await fetch_json(client, "https://www.okx.com/api/v5/market/candles",
                                    {"instId": "BTC-USDT", "bar": "1H", "limit": "60"})
            btc = indicators(rows.get("data", []), "OKX")
            if btc:
                btc_state = "BULLISH" if btc["price"] > btc["ema20"] > btc["ema50"] else "BEARISH"
                if btc_state != "BULLISH" and btc["price"] > btc["ema20"]:
                    btc_state = "SIDEWAYS"
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        sem = asyncio.Semaphore(5)

        async def analyze(item):
            ticker, instrument, quote_volume = item
            async with sem:
                try:
                    inst_id = instrument["instId"]
                    candles = await fetch_json(client, "https://www.okx.com/api/v5/market/candles",
                                               {"instId": inst_id, "bar": "1H", "limit": "60"})
                    data = indicators(candles.get("data", []), "OKX")
                    if not data:
                        return None
                    price = float(ticker.get("last") or data["price"])
                    data["price"] = price
                    scored = score_asset(data, quote_volume)
                    base = instrument.get("baseCcy") or inst_id.split("-")[0]
                    symbol = inst_id.replace("-", "")
                    btc_allowed = btc_state == "BULLISH"
                    confirmed = (
                        scored["score"] >= 83 and price > data["ema20"] > data["ema50"]
                        and data["change24"] >= 1.0 and data["volume_ratio"] >= 1.2
                        and quote_volume >= min_volume
                    )
                    return {
                        "symbol": symbol, "name": base, "exchange": "OKX Spot",
                        "source": "okx", "quote_currency": "USDT", "price": price,
                        "change_24h_pct": round(data["change24"], 2),
                        "quote_volume_24h": round(quote_volume, 2),
                        "trade_ready": True, "exchange_pair_verified": True,
                        "volume_confirmed": data["volume_ratio"] >= 1.2,
                        "btc_buy_allowed": btc_allowed,
                        "signal": "BUY" if confirmed and btc_allowed else "HOLD",
                        "early_signal": "EARLY BUY" if scored["score"] >= 75 and not (confirmed and btc_allowed) else None,
                        "stop_loss": round(price - 2.0 * data["atr"], 10),
                        "take_profit": round(price + 4.0 * data["atr"], 10),
                        **scored,
                    }
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    return None

        results = await asyncio.gather(*(analyze(x) for x in eligible[:max(limit * 3, 60)]))
        assets = sorted([x for x in results if x], key=lambda x: x["score"], reverse=True)[:limit]
        return {
            "source": "OKX public spot market data", "exchange": "OKX Spot",
            "scanned": len(assets), "btc_market_filter": {
                "available": btc_state != "UNKNOWN", "buy_allowed": btc_state == "BULLISH",
                "market_state": btc_state
            }, "updated_at": datetime.now(timezone.utc).isoformat(), "assets": assets,
            "notice": "Только публичные спотовые рынки OKX USDT. Сделки в приложении — PAPER, реальные ордера не отправляются."
        }


async def cached_scan(key: str, scan):
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    try:
        payload = await scan()
        _cache[key] = (time.time(), payload)
        return payload
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"{key} market data temporarily unavailable: {type(exc).__name__}") from exc


@router.get("/api/v1/crypto/coinbase/scan")
async def scan_coinbase_route(
    limit: int = Query(30, ge=5, le=50),
    min_volume: float = Query(1_000_000, ge=0),
):
    return await cached_scan(f"coinbase:{limit}:{min_volume}", lambda: scan_coinbase(limit, min_volume))


@router.get("/api/v1/crypto/okx/scan")
async def scan_okx_route(
    limit: int = Query(30, ge=5, le=50),
    min_volume: float = Query(1_000_000, ge=0),
):
    return await cached_scan(f"okx:{limit}:{min_volume}", lambda: scan_okx(limit, min_volume))
