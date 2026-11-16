import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
CACHE_TTL_SECONDS = 45
_cache: tuple[float, dict[str, Any]] | None = None

_STABLECOINS = {
    "USDT", "USDC", "USDE", "USDS", "DAI", "FDUSD", "TUSD", "USDP",
    "PYUSD", "GUSD", "FRAX", "USDD", "UST", "USTC", "USDX", "USDJ",
    "USD1", "RLUSD", "BFUSD", "XUSD", "EURC", "EURS", "USDTB",
}


def summarize_market_breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure rising/falling 24h spot markets directly from Bybit Live Market."""
    by_symbol: dict[str, float | None] = {}
    for ticker in rows:
        if not isinstance(ticker, dict):
            continue
        symbol = str(ticker.get("symbol") or "").strip().upper()
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if not base or base in _STABLECOINS:
            continue
        raw_change = ticker.get("price24hPcnt")
        change: float | None = None
        if raw_change is not None:
            try:
                parsed = float(raw_change) * 100.0
                if math.isfinite(parsed):
                    change = parsed
            except (TypeError, ValueError):
                pass
        by_symbol.setdefault(symbol, change)

    total = len(by_symbol)
    measured = [value for value in by_symbol.values() if value is not None]
    rising = sum(value > 0 for value in measured)
    falling = sum(value < 0 for value in measured)
    unchanged = sum(value == 0 for value in measured)
    missing = total - len(measured)
    count = len(measured)
    rising_pct = rising / count * 100 if count else 0.0
    falling_pct = falling / count * 100 if count else 0.0
    allow_long = count > 0 and rising_pct >= 50.0
    allow_short = count > 0 and falling_pct >= 50.0

    if count == 0:
        status = "UNAVAILABLE"
        reason = "Bybit Live Market не вернул достоверные изменения цен за 24 часа."
    elif allow_long and allow_short:
        status = "BALANCED_50_50"
        reason = "По данным Bybit одновременно достигнут порог 50% для LONG и SHORT."
    elif allow_long:
        status = "LONG_BIAS"
        reason = "Не менее 50% измеренных спотовых пар Bybit растут за 24 часа; разрешён LONG."
    elif allow_short:
        status = "SHORT_BIAS"
        reason = "Не менее 50% измеренных спотовых пар Bybit падают за 24 часа; разрешён SHORT."
    else:
        status = "WAIT"
        reason = "По данным Bybit ни рост, ни падение не достигли 50%; новые входы заблокированы."

    return {
        "available": total > 0,
        "source": "Bybit Live Market Spot tickers (USDT, 24h)",
        "returned_assets": len(rows),
        "total_coins": total,
        "measured_coins": count,
        "missing_change_coins": missing,
        "rising_coins": rising,
        "falling_coins": falling,
        "unchanged_coins": unchanged,
        "rising_pct": round(rising_pct, 2),
        "falling_pct": round(falling_pct, 2),
        "unchanged_pct": round(unchanged / count * 100, 2) if count else 0.0,
        "status": status,
        "allow_new_entries": allow_long,
        "allow_long_entries": allow_long,
        "allow_short_entries": allow_short,
        "threshold_pct": 50.0,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_market_breadth() -> dict[str, Any]:
    """Fetch the Bybit Live Market spot ticker universe for the 50% direction filter."""
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "HAN-INVEST/0.7", "Accept": "application/json"},
        ) as client:
            response = await client.get(
                BYBIT_TICKERS_URL,
                params={"category": "spot"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("retCode") != 0:
                raise ValueError("Bybit returned an invalid spot ticker response")
            result_data = payload.get("result") or {}
            rows = result_data.get("list") or []
            if not isinstance(rows, list):
                raise ValueError("Bybit returned an invalid ticker list")
            result = summarize_market_breadth(rows)
            if not result["available"]:
                raise ValueError("Bybit returned no usable spot market breadth data")
            _cache = (time.time(), result)
            return result
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Данные Bybit Live Market для фильтра 50% временно недоступны.",
        ) from exc


@router.get("/api/v1/crypto/market-breadth")
async def market_breadth():
    return await get_market_breadth()
