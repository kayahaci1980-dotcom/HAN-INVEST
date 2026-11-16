import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
FREE_SOURCE_URL = "https://api.coinpaprika.com/v1/tickers"
PAID_SOURCE_URL = "https://api-pro.coinpaprika.com/v1/tickers"
CACHE_TTL_SECONDS = 60
_cache: tuple[float, dict[str, Any]] | None = None

_STABLECOINS = {
    "USDT", "USDC", "USDE", "USDS", "DAI", "FDUSD", "TUSD", "USDP",
    "PYUSD", "GUSD", "FRAX", "USDD", "UST", "USTC", "USDX", "USDJ",
    "USD1", "RLUSD", "BFUSD", "XUSD", "EURC", "EURS", "USDTB",
}


def summarize_market_breadth(rows: list[dict[str, Any]], *, full_catalogue: bool = False) -> dict[str, Any]:
    """Count every unique non-stable active asset; track missing 24h changes separately."""
    by_id: dict[str, tuple[str, float | None]] = {}
    for coin in rows:
        if not isinstance(coin, dict):
            continue
        coin_id = str(coin.get("id") or "").strip()
        symbol = str(coin.get("symbol") or "").strip().upper()
        if not coin_id or not symbol or symbol in _STABLECOINS:
            continue
        quote = (coin.get("quotes") or {}).get("USD") or {}
        change = quote.get("percent_change_24h")
        change_value: float | None = None
        if change is not None:
            try:
                parsed = float(change)
                if math.isfinite(parsed):
                    change_value = parsed
            except (TypeError, ValueError):
                pass
        # Keep the asset in the market total even when its 24h change is absent.
        by_id.setdefault(coin_id, (symbol, change_value))

    total = len(by_id)
    measured = [change for _, change in by_id.values() if change is not None]
    rising = sum(1 for change in measured if change > 0)
    falling = sum(1 for change in measured if change < 0)
    unchanged = sum(1 for change in measured if change == 0)
    missing = total - len(measured)
    measured_total = len(measured)

    rising_pct = rising / measured_total * 100 if measured_total else 0.0
    falling_pct = falling / measured_total * 100 if measured_total else 0.0
    allow_long_entries = measured_total > 0 and rising_pct >= 50.0
    allow_short_entries = measured_total > 0 and falling_pct >= 50.0

    if measured_total == 0:
        status = "UNAVAILABLE"
        reason = "Нет достоверных данных по изменению цен за 24 часа."
    elif allow_long_entries and allow_short_entries:
        status = "BALANCED_50_50"
        reason = "Растущие и падающие монеты одновременно достигли порога 50%; разрешены LONG и SHORT."
    elif allow_long_entries:
        status = "LONG_BIAS"
        reason = "Растут 50% или больше монет с доступным изменением цены за 24 часа; разрешён LONG."
    elif allow_short_entries:
        status = "SHORT_BIAS"
        reason = "Падают 50% или больше монет с доступным изменением цены за 24 часа; разрешён SHORT."
    else:
        status = "WAIT"
        reason = "Ни растущие, ни падающие монеты не достигли порога 50%; новые входы заблокированы."

    return {
        "available": total > 0,
        "source": "CoinPaprika global tickers (USD, 24h)",
        "attribution": "Powered by CoinPaprika",
        "license_note": "Free API is for personal use only; paid self-serve plans cover internal commercial use. Redistribution to customers requires Enterprise.",
        "deduplication": "unique_asset_id",
        "plan_mode": "paid_all_available" if full_catalogue else "free_up_to_2000",
        "returned_assets": len(rows),
        "full_catalogue_coverage": full_catalogue,
        "coverage_note": (
            "Получен весь доступный каталог CoinPaprika по платному API-плану."
            if full_catalogue
            else "Бесплатный API возвращает до 2 000 активов CoinPaprika; это не весь каталог."
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_coins": total,
        "measured_coins": measured_total,
        "missing_change_coins": missing,
        "rising_coins": rising,
        "falling_coins": falling,
        "unchanged_coins": unchanged,
        "rising_pct": round(rising / measured_total * 100, 2) if measured_total else 0.0,
        "falling_pct": round(falling / measured_total * 100, 2) if measured_total else 0.0,
        "unchanged_pct": round(unchanged / measured_total * 100, 2) if measured_total else 0.0,
        "status": status,
        "allow_new_entries": allow_long_entries,
        "allow_long_entries": allow_long_entries,
        "allow_short_entries": allow_short_entries,
        "threshold_pct": 50.0,
        "reason": reason,
    }


async def get_market_breadth() -> dict[str, Any]:
    """Use the largest catalogue allowed by the configured CoinPaprika plan."""
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    api_key = os.getenv("COINPAPRIKA_API_KEY", "").strip()
    full_catalogue = bool(api_key)
    source_url = PAID_SOURCE_URL if full_catalogue else FREE_SOURCE_URL
    headers = {"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers=headers,
        ) as client:
            response = await client.get(source_url, params={"quotes": "USD"})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("CoinPaprika returned an unexpected response")
            result = summarize_market_breadth(rows, full_catalogue=full_catalogue)
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
