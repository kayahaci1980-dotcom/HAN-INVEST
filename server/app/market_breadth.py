import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
SOURCE_URL = "https://api.coinpaprika.com/v1/tickers"
SOURCE_LIMIT = 2000
CACHE_TTL_SECONDS = 60
_cache: tuple[float, dict[str, Any]] | None = None

_STABLECOINS = {
    "USDT", "USDC", "USDE", "USDS", "DAI", "FDUSD", "TUSD", "USDP",
    "PYUSD", "GUSD", "FRAX", "USDD", "UST", "USTC", "USDX", "USDJ",
    "USD1", "RLUSD", "BFUSD", "XUSD", "EURC", "EURS", "USDTB",
}


def summarize_market_breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count each CoinPaprika asset ID once; exclude stablecoins by symbol."""
    by_id: dict[str, tuple[str, float]] = {}
    for coin in rows:
        if not isinstance(coin, dict):
            continue
        coin_id = str(coin.get("id") or "").strip()
        symbol = str(coin.get("symbol") or "").strip().upper()
        quote = (coin.get("quotes") or {}).get("USD") or {}
        change = quote.get("percent_change_24h")
        if not coin_id or not symbol or symbol in _STABLECOINS or change is None:
            continue
        try:
            change_value = float(change)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(change_value):
            continue
        by_id.setdefault(coin_id, (symbol, change_value))

    total = len(by_id)
    rising = sum(1 for _, change in by_id.values() if change > 0)
    falling = sum(1 for _, change in by_id.values() if change < 0)
    unchanged = total - rising - falling

    if total == 0:
        status, allow_new_entries = "UNAVAILABLE", False
        reason = "Нет достоверных данных по изменению цен за 24 часа."
    elif rising > total / 2:
        status, allow_new_entries = "ALLOW_NEW_BUYS", True
        reason = "Растёт больше половины уникальных криптовалют рынка."
    elif falling > total / 2:
        status, allow_new_entries = "BLOCK_NEW_BUYS", False
        reason = "Падает больше половины уникальных криптовалют рынка."
    else:
        status, allow_new_entries = "WAIT", False
        reason = "Ни растущие, ни падающие монеты не составляют больше половины рынка."

    return {
        "available": total > 0,
        "source": "CoinPaprika global tickers (USD, 24h)",
        "attribution": "Powered by CoinPaprika",
        "license_note": "Free API access is for personal use; commercial use requires an appropriate paid plan.",
        "deduplication": "unique_asset_id",
        "requested_limit": SOURCE_LIMIT,
        "returned_assets": len(rows),
        "full_catalogue_coverage": False,
        "coverage_note": "Используется до 2 000 активов CoinPaprika; это широкий рыночный охват, но не весь каталог.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_coins": total,
        "rising_coins": rising,
        "falling_coins": falling,
        "unchanged_coins": unchanged,
        "rising_pct": round(rising / total * 100, 2) if total else 0.0,
        "falling_pct": round(falling / total * 100, 2) if total else 0.0,
        "unchanged_pct": round(unchanged / total * 100, 2) if total else 0.0,
        "status": status,
        "allow_new_entries": allow_new_entries,
        "reason": reason,
    }


async def get_market_breadth() -> dict[str, Any]:
    """CoinPaprika failure is fail-closed for new entries."""
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"},
        ) as client:
            response = await client.get(
                SOURCE_URL, params={"quotes": "USD", "limit": SOURCE_LIMIT}
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("CoinPaprika returned an unexpected response")
            result = summarize_market_breadth(rows)
            if not result["available"]:
                raise ValueError("CoinPaprika returned no valid market breadth data")
            _cache = (time.time(), result)
            return result
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Глобальные данные роста и падения криптовалют временно недоступны.",
        ) from exc


@router.get("/api/v1/crypto/market-breadth")
async def market_breadth():
    return await get_market_breadth()
