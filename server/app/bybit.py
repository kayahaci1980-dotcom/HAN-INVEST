import asyncio
import math
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
BASE = "https://api.bybit.com"
CACHE_TTL = 45
_cache: tuple[float, dict[str, Any]] | None = None


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def get_json(client: httpx.AsyncClient, path: str, params: dict):
    response = await client.get(f"{BASE}{path}", params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("retCode") != 0:
        raise ValueError(str(data.get("retMsg", "Bybit API error")))
    return data.get("result", {})


async def analyze(client: httpx.AsyncClient, ticker: dict, instrument: dict, btc_state: str):
    symbol = ticker.get("symbol", "")
    try:
        result = await get_json(client, "/v5/market/kline", {
            "category": "spot", "symbol": symbol, "interval": "60", "limit": "60"
        })
        rows = list(reversed(result.get("list", [])))
        rows = rows[:-1]
        if len(rows) < 50:
            return None
        closes = [float(row[4]) for row in rows]
        highs = [float(row[2]) for row in rows]
        lows = [float(row[3]) for row in rows]
        volumes = [float(row[5]) for row in rows]
        price = float(ticker.get("lastPrice") or closes[-1])
        ma20, ma50 = mean(closes[-20:]), mean(closes[-50:])
        change6 = (closes[-1] / closes[-7] - 1) * 100 if closes[-7] else 0.0
        change24 = float(ticker.get("price24hPcnt") or 0) * 100
        vol_ratio = volumes[-1] / mean(volumes[-25:-1]) if mean(volumes[-25:-1]) else 0.0
        quote_volume = float(ticker.get("turnover24h") or 0)
        tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
        atr = mean(tr[-14:])
        trend = 20 if price > ma20 > ma50 else 8
        momentum = max(0, min(20, round(10 + change24 * 0.8)))
        short = max(0, min(20, round(10 + change6 * 1.2)))
        liquidity = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
        volume_score = 20 if vol_ratio >= 1.2 else 8
        score = min(100, trend + momentum + short + liquidity + volume_score)
        setup = price > ma20 > ma50 and score >= 80 and change6 >= 1 and vol_ratio >= 1.2 and quote_volume >= 1_000_000
        signal = "BUY" if setup and btc_state == "BULLISH" else "WATCH" if score >= 55 else "HOLD"
        early = "EARLY BUY" if setup and btc_state == "SIDEWAYS" else None
        return {
            "symbol": symbol, "name": symbol[:-4], "exchange": "Bybit Spot",
            "source": "bybit", "price": price, "change_24h_pct": round(change24, 2),
            "momentum_6h_pct": round(change6, 2), "quote_volume_24h": round(quote_volume, 2),
            "volume_ratio": round(vol_ratio, 2), "volume_confirmed": vol_ratio >= 1.2,
            "ma20": round(ma20, 8), "ma50": round(ma50, 8), "atr_14": round(atr, 8),
            "score": score, "signal": signal, "early_signal": early,
            "trade_ready": instrument.get("status") == "Trading",
            "pair_status": instrument.get("status"), "quote_currency": "USDT",
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return None


@router.get("/api/v1/crypto/bybit/scan")
async def scan_bybit(limit: int = Query(30, ge=1, le=100), min_volume: float = Query(1_000_000, ge=0)):
    global _cache
    import time
    if _cache and time.time() - _cache[0] < CACHE_TTL and _cache[1].get("limit") == limit:
        return _cache[1]
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
            instruments_data, tickers_data = await asyncio.gather(
                get_json(client, "/v5/market/instruments-info", {"category": "spot", "limit": "1000"}),
                get_json(client, "/v5/market/tickers", {"category": "spot"}),
            )
            instruments = {x.get("symbol"): x for x in instruments_data.get("list", [])
                           if x.get("quoteCoin") == "USDT" and x.get("status") == "Trading"}
            tickers = [x for x in tickers_data.get("list", [])
                       if x.get("symbol") in instruments and float(x.get("turnover24h") or 0) >= min_volume]
            btc = next((x for x in tickers if x.get("symbol") == "BTCUSDT"), None)
            btc_state = "UNKNOWN"
            btc_info = {"available": False, "buy_allowed": False, "market_state": "UNKNOWN"}
            if btc:
                btc_rows = await get_json(client, "/v5/market/kline", {
                    "category": "spot", "symbol": "BTCUSDT", "interval": "60", "limit": "60"
                })
                rows = list(reversed(btc_rows.get("list", [])))[:-1]
                closes = [float(x[4]) for x in rows]
                if len(closes) >= 50:
                    ma20, ma50 = mean(closes[-20:]), mean(closes[-50:])
                    price = float(btc.get("lastPrice") or closes[-1])
                    btc_state = "BULLISH" if price > ma20 > ma50 else "BEARISH" if price < ma20 < ma50 else "SIDEWAYS"
                    btc_info = {"available": True, "buy_allowed": btc_state == "BULLISH",
                                "market_state": btc_state, "price": price, "ma20": round(ma20, 2),
                                "ma50": round(ma50, 2)}
            selected = sorted(tickers, key=lambda x: float(x.get("turnover24h") or 0), reverse=True)[:limit]
            semaphore = asyncio.Semaphore(5)
            async def limited(item):
                async with semaphore:
                    return await analyze(client, item, instruments[item["symbol"]], btc_state)
            assets = [x for x in await asyncio.gather(*(limited(x) for x in selected)) if x]
            assets.sort(key=lambda x: x["score"], reverse=True)
            for rank, asset in enumerate(assets, 1):
                asset["market_rank"] = rank
            payload = {"source": "Bybit public market data", "exchange": "Bybit Spot",
                       "scanned": len(assets), "eligible_pairs": len(instruments),
                       "btc_market_filter": btc_info, "updated_at": datetime.now(timezone.utc).isoformat(),
                       "assets": assets, "limit": limit,
                       "notice": "Только спотовые пары Bybit USDT. Индикаторы рассчитаны по закрытым часовым свечам. BUY требует BULLISH BTC и всех фильтров актива."}
            _cache = (time.time(), payload)
            return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Данные Bybit Spot временно недоступны.") from exc
