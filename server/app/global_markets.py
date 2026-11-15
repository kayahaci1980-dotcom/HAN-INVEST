import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_SYMBOLS = [
    ("EUR/USD", "EURUSD=X", "Валюты"),
    ("GBP/USD", "GBPUSD=X", "Валюты"),
    ("USD/JPY", "USDJPY=X", "Валюты"),
    ("USD/CHF", "USDCHF=X", "Валюты"),
    ("USD/CNY", "USDCNY=X", "Валюты"),
    ("USD/KGS", "USDKGS=X", "Валюты"),
    ("Золото", "GC=F", "Металлы"),
    ("Серебро", "SI=F", "Металлы"),
    ("Платина", "PL=F", "Металлы"),
    ("Нефть Brent", "BZ=F", "Энергия"),
    ("Нефть WTI", "CL=F", "Энергия"),
    ("Природный газ", "NG=F", "Энергия"),
    ("S&P 500", "^GSPC", "Индексы"),
    ("NASDAQ", "^IXIC", "Индексы"),
    ("Dow Jones", "^DJI", "Индексы"),
    ("FTSE 100", "^FTSE", "Индексы"),
    ("Nikkei 225", "^N225", "Индексы"),
    ("DXY — индекс доллара", "DX-Y.NYB", "Индексы"),
]

async def _quote(client: httpx.AsyncClient, item: tuple[str, str, str]) -> dict:
    name, symbol, category = item
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        response = await client.get(url, params={"range": "5d", "interval": "1d"})
        response.raise_for_status()
        payload = response.json()["chart"]["result"][0]
        meta = payload.get("meta", {})
        indicators = payload.get("indicators", {}).get("quote", [{}])[0]
        closes = [x for x in indicators.get("close", []) if x is not None]
        price = meta.get("regularMarketPrice")
        previous = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None and closes:
            price = closes[-1]
        if previous is None and len(closes) >= 2:
            previous = closes[-2]
        change = ((float(price) / float(previous)) - 1) * 100 if price and previous else None
        return {
            "name": name, "symbol": symbol, "category": category,
            "price": price, "currency": meta.get("currency", "USD"),
            "change_pct": change, "market_time": meta.get("regularMarketTime"),
            "available": price is not None,
        }
    except Exception:
        return {
            "name": name, "symbol": symbol, "category": category,
            "price": None, "currency": "USD", "change_pct": None,
            "market_time": None, "available": False,
        }

@router.get("/api/v1/global-markets")
async def global_markets():
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < 60:
        return _CACHE["data"]
    headers = {"User-Agent": "Mozilla/5.0 HAN-INVEST", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        results = await asyncio.gather(*[_quote(client, item) for item in _SYMBOLS])
    available = sum(1 for item in results if item["available"])
    if available == 0:
        raise HTTPException(status_code=503, detail="Глобальный финансовый источник временно недоступен")
    data = {
        "source": "Yahoo Finance market data",
        "updated_at": int(time.time()),
        "refresh_seconds": 60,
        "available": available,
        "total": len(results),
        "assets": results,
    }
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data
