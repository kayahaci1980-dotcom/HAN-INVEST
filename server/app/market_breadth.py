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

    if measured_total == 0:
        status, allow_new_entries = "UNAVAILABLE", False
        reason = "Нет достоверных данных по изменению цен за 24 часа."
    elif rising > measured_total / 2:
        status, allow_new_entries = "ALLOW_NEW_BUYS", True
        reason = "Растёт больше половины монет с доступным изменением цены за 24 часа."
    elif falling > measured_total / 2:
        status, allow_new_entries = "BLOCK_NEW_BUYS", False
        reason = "Падает больше половины монет с доступным изменением цены за 24 часа."
    else:
        status, allow_new_entries = "WAIT", False
        reason = "Ни растущие, ни падающие монеты с доступными данными не составляют больше половины."

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
        "allow_new_entries": allow_new_entries,
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


_assets_cache: tuple[float, dict[str, Any]] | None = None


async def get_global_market_assets(limit: int = 2000) -> dict[str, Any]:
    """Return a deduplicated read-only global asset catalogue, separate from exchange trading candidates."""
    global _assets_cache
    if _assets_cache and time.time() - _assets_cache[0] < CACHE_TTL_SECONDS:
        cached = _assets_cache[1]
        return {**cached, "assets": cached["assets"][:limit], "returned_assets": min(limit, len(cached["assets"]))}

    api_key = os.getenv("COINPAPRIKA_API_KEY", "").strip()
    full_catalogue = bool(api_key)
    source_url = PAID_SOURCE_URL if full_catalogue else FREE_SOURCE_URL
    headers = {"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), headers=headers) as client:
            response = await client.get(source_url, params={"quotes": "USD"})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("CoinPaprika returned an unexpected response")
            by_id: dict[str, dict[str, Any]] = {}
            for coin in rows:
                if not isinstance(coin, dict):
                    continue
                coin_id = str(coin.get("id") or "").strip()
                symbol = str(coin.get("symbol") or "").strip().upper()
                name = str(coin.get("name") or "").strip()
                if not coin_id or not symbol or symbol in _STABLECOINS or coin_id in by_id:
                    continue
                quote = (coin.get("quotes") or {}).get("USD") or {}
                def finite_number(value):
                    try:
                        number = float(value)
                        return number if math.isfinite(number) else None
                    except (TypeError, ValueError):
                        return None
                by_id[coin_id] = {
                    "id": coin_id,
                    "symbol": symbol,
                    "name": name or symbol,
                    "rank": coin.get("rank"),
                    "price_usd": finite_number(quote.get("price")),
                    "change_24h_pct": finite_number(quote.get("percent_change_24h")),
                    "volume_24h_usd": finite_number(quote.get("volume_24h")),
                    "market_cap_usd": finite_number(quote.get("market_cap")),
                }
            assets = sorted(by_id.values(), key=lambda item: (
                item["rank"] is None, item["rank"] if item["rank"] is not None else 10**9
            ))
            result = {
                "available": bool(assets),
                "source": "CoinPaprika global tickers (USD)",
                "plan_mode": "paid_all_available" if full_catalogue else "free_up_to_2000",
                "coverage_note": "До 2 000 активов в бесплатном каталоге; платный API может вернуть больше.",
                "total_assets_available": len(assets),
                "returned_assets": min(limit, len(assets)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "assets": assets,
            }
            _assets_cache = (time.time(), result)
            return {**result, "assets": assets[:limit]}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Глобальный список криптовалют временно недоступен.",
        ) from exc


@router.get("/api/v1/crypto/market-assets")
async def market_assets(limit: int = 2000):
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=422, detail="limit должен быть от 1 до 5000")
    return await get_global_market_assets(limit)
