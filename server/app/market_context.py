import time
import xml.etree.ElementTree as ET
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


def _market_state(change: float) -> tuple[str, str]:
    if change >= 1.0:
        return "BULLISH", "Рынок растёт"
    if change <= -1.0:
        return "BEARISH", "Рынок снижается"
    return "SIDEWAYS", "Рынок без выраженного направления"


async def _fetch_global_market(client: httpx.AsyncClient) -> dict:
    errors = []
    # Primary source
    try:
        response = await client.get("https://api.coingecko.com/api/v3/global")
        response.raise_for_status()
        data = response.json().get("data", {})
        caps = data.get("total_market_cap") or {}
        volume = data.get("total_volume") or {}
        dominance = (data.get("market_cap_percentage") or {}).get("btc")
        change = data.get("market_cap_change_percentage_24h_usd")
        if dominance is None or change is None or not caps.get("usd") or not volume.get("usd"):
            raise ValueError("CoinGecko global response is incomplete")
        change = float(change)
        state, label = _market_state(change)
        return {
            "available": True, "source": "CoinGecko Global",
            "btc_dominance_pct": round(float(dominance), 2),
            "total_market_cap_usd": round(float(caps["usd"]), 2),
            "total_volume_24h_usd": round(float(volume["usd"]), 2),
            "market_cap_change_24h_pct": round(change, 2),
            "market_state": state, "market_state_label": label,
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        errors.append("CoinGecko: " + type(exc).__name__)

    # Independent fallback source
    try:
        response = await client.get("https://api.coinpaprika.com/v1/global")
        response.raise_for_status()
        data = response.json()
        caps = data.get("market_cap_usd")
        volume = data.get("volume_24h_usd")
        dominance = data.get("bitcoin_dominance_percentage")
        change = data.get("market_cap_change_24h")
        if caps is None or volume is None or dominance is None or change is None:
            raise ValueError("CoinPaprika global response is incomplete")
        change = float(change)
        state, label = _market_state(change)
        return {
            "available": True, "source": "CoinPaprika Global (резервный источник)",
            "btc_dominance_pct": round(float(dominance), 2),
            "total_market_cap_usd": round(float(caps), 2),
            "total_volume_24h_usd": round(float(volume), 2),
            "market_cap_change_24h_pct": round(change, 2),
            "market_state": state, "market_state_label": label,
            "fallback_used": True,
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        errors.append("CoinPaprika: " + type(exc).__name__)

    return {
        "available": False, "source": "CoinGecko Global + CoinPaprika Global",
        "message": "Глобальные данные временно недоступны.",
        "source_errors": errors,
    }


def _rss_articles(xml_text: str, source_name: str, limit: int = 10) -> list[dict]:
    root = ET.fromstring(xml_text)
    articles = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if title and url:
            articles.append({
                "title": title[:300], "url": url, "source": source_name,
                "published_at": published,
                "sentiment": _headline_sentiment(title),
            })
        if len(articles) >= limit:
            break
    return articles


async def _fetch_news(client: httpx.AsyncClient) -> dict:
    errors = []
    try:
        response = await client.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query": "(cryptocurrency OR crypto OR Bitcoin OR Ethereum)",
                    "mode": "ArtList", "format": "json", "sort": "DateDesc", "maxrecords": 12},
        )
        response.raise_for_status()
        raw = response.json()
        articles = []
        for item in raw.get("articles", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = item.get("url")
            if title and url:
                articles.append({
                    "title": title[:300], "url": url,
                    "source": item.get("domain") or item.get("sourceCountry") or "GDELT",
                    "published_at": item.get("seendate"),
                    "sentiment": _headline_sentiment(title),
                })
        if articles:
            return {"available": True, "source": "GDELT", "count": len(articles), "articles": articles[:10]}
        errors.append("GDELT: no articles")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        errors.append("GDELT: " + type(exc).__name__)

    try:
        response = await client.get("https://www.coindesk.com/arc/outboundfeeds/rss/")
        response.raise_for_status()
        articles = _rss_articles(response.text, "CoinDesk RSS")
        if articles:
            return {
                "available": True, "source": "CoinDesk RSS (резервный источник)",
                "count": len(articles), "articles": articles, "fallback_used": True,
            }
        errors.append("CoinDesk RSS: no articles")
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
        errors.append("CoinDesk RSS: " + type(exc).__name__)

    return {
        "available": False, "source": "GDELT + CoinDesk RSS", "articles": [],
        "message": "Новостная лента временно недоступна.", "source_errors": errors,
    }


@router.get("/api/v1/crypto/market-context")
async def crypto_market_context():
    global _CACHE
    if _CACHE and time.time() - _CACHE[0] < _CACHE_TTL:
        return _CACHE[1]

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": "HAN-INVEST/1.0", "Accept": "*/*"},
        follow_redirects=True,
    ) as client:
        market, news = await __import__("asyncio").gather(
            _fetch_global_market(client), _fetch_news(client)
        )

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "news": news,
        "notice": (
            "Источники: CoinGecko/CoinPaprika и GDELT/CoinDesk RSS. "
            "Тональность — предварительная оценка слов в заголовке, не анализ смысла "
            "и не самостоятельный торговый сигнал. Данные рынка предназначены для контекста."
        ),
    }
    _CACHE = (time.time(), result)
    return result
