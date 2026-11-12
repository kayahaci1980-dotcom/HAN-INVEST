import time
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter

router = APIRouter()
_CACHE: tuple[float, dict] | None = None
_CACHE_TTL = 300
POSITIVE = ("surge", "rally", "gain", "growth", "approval", "adoption", "inflow", "上涨", "рост", "одобр", "приток")
NEGATIVE = ("crash", "plunge", "hack", "exploit", "lawsuit", "ban", "outflow", "паден", "обвал", "взлом", "запрет", "отток")

def _headline_sentiment(title: str) -> str:
    text = title.lower()
    positive = sum(1 for word in POSITIVE if word in text)
    negative = sum(1 for word in NEGATIVE if word in text)
    if positive > negative:
        return "Позитивный заголовок"
    if negative > positive:
        return "Негативный заголовок"
    return "Нейтральный / неясный заголовок"

@router.get("/api/v1/crypto/market-context")
async def crypto_market_context():
    global _CACHE
    if _CACHE and time.time() - _CACHE[0] < _CACHE_TTL:
        return _CACHE[1]
    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "market": {"available": False, "source": "CoinGecko Global"},
        "news": {"available": False, "source": "GDELT", "articles": []},
        "notice": "Тональность новостей определяется простым анализом слов в заголовках, не является полноценной оценкой смысла и не используется как самостоятельный сигнал BUY."
    }
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
        try:
            response = await client.get("https://api.coingecko.com/api/v3/global")
            response.raise_for_status()
            data = response.json().get("data", {})
            caps, volume = data.get("total_market_cap", {}), data.get("total_volume", {})
            dominance = (data.get("market_cap_percentage") or {}).get("btc")
            change = data.get("market_cap_change_percentage_24h_usd")
            change = float(change) if change is not None else None
            if change is not None and dominance is not None:
                state = "BULLISH" if change >= 1.0 else "BEARISH" if change <= -1.0 else "SIDEWAYS"
                result["market"] = {
                    "available": True, "source": "CoinGecko Global",
                    "btc_dominance_pct": round(float(dominance), 2),
                    "total_market_cap_usd": round(float(caps.get("usd") or 0), 2),
                    "total_volume_24h_usd": round(float(volume.get("usd") or 0), 2),
                    "market_cap_change_24h_pct": round(change, 2),
                    "market_state": state,
                    "market_state_label": "Рынок растёт" if state == "BULLISH" else "Рынок снижается" if state == "BEARISH" else "Рынок без выраженного направления"
                }
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            result["market"]["message"] = "Данные глобального рынка временно недоступны."
        try:
            response = await client.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={"query": "(cryptocurrency OR crypto OR Bitcoin OR Ethereum)",
                        "mode": "ArtList", "format": "json", "sort": "DateDesc", "maxrecords": 12}
            )
            response.raise_for_status()
            raw = response.json()
            articles = []
            for item in raw.get("articles", []) if isinstance(raw, dict) else []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if title:
                    articles.append({
                        "title": title[:300], "url": item.get("url"),
                        "source": item.get("domain") or item.get("sourceCountry") or "GDELT",
                        "published_at": item.get("seendate"),
                        "sentiment": _headline_sentiment(title)
                    })
            result["news"] = {"available": True, "source": "GDELT", "count": len(articles), "articles": articles[:10]}
        except (httpx.HTTPError, ValueError, TypeError):
            result["news"]["message"] = "Новостная лента временно недоступна."
    _CACHE = (time.time(), result)
    return result
