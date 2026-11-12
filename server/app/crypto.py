import asyncio
import math
import logging
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)
# Binance documents this host specifically for public market-data-only requests.
BASE_URL = "https://data-api.binance.vision"
CACHE_TTL = 60
_cache: dict[str, tuple[float, Any]] = {}
_semaphore = asyncio.Semaphore(2)
_blocked_until = 0.0


class BinanceCooldownError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Binance rate limit; retry after {retry_after}s")


def _retry_after_seconds(value: str | None) -> int:
    if value:
        try:
            return max(1, int(float(value)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(1, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return 300

EXCLUDED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
# Binance-listed tokenized equity/ETF symbols are not ordinary crypto assets.
EXCLUDED_NON_CRYPTO_BASE_ASSETS = {
    "GOOGLB", "SNDKB", "SOXSB", "MUB", "INTCB", "NVDAB", "SPCXB", "SOXLB",
}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _score_change(value: float, scale: float = 5.0) -> int:
    return max(0, min(20, round(10 + value * scale)))


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None):
    global _blocked_until
    async with _semaphore:
        remaining = int(_blocked_until - time.time())
        if remaining > 0:
            raise BinanceCooldownError(remaining)

        response = await client.get(f"{BASE_URL}{path}", params=params)
        if response.status_code in (418, 429):
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            _blocked_until = max(_blocked_until, time.time() + retry_after)
            logger.warning(
                "Binance returned HTTP %s; pausing requests for %s seconds",
                response.status_code,
                retry_after,
            )
            raise BinanceCooldownError(retry_after)

        response.raise_for_status()
        return response.json()


async def _analyze_symbol(client: httpx.AsyncClient, ticker: dict, pair_info: dict):
    symbol = ticker.get("symbol", "")
    pair_verified = (
        pair_info.get("status") == "TRADING"
        and pair_info.get("isSpotTradingAllowed") is True
        and pair_info.get("quoteAsset") == "USDT"
    )
    try:
        candles = await _get_json(
            client,
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1h", "limit": 60},
        )
        closes = [float(row[4]) for row in candles if len(row) > 4]
        volumes = [float(row[5]) for row in candles if len(row) > 5]
        if len(closes) < 50:
            return None

        last = closes[-1]
        ma20 = _mean(closes[-20:])
        ma50 = _mean(closes[-50:])
        change_6h = ((last / closes[-7]) - 1) * 100 if closes[-7] else 0
        change_24h = ((last / closes[-25]) - 1) * 100 if closes[-25] else 0
        returns = [
            (closes[i] / closes[i - 1] - 1)
            for i in range(1, len(closes)) if closes[i - 1]
        ]
        hourly_volatility = math.sqrt(_mean([x * x for x in returns])) * 100
        quote_volume = float(ticker.get("quoteVolume", 0))
        volume_ratio = (
            volumes[-1] / _mean(volumes[-25:-1])
            if len(volumes) >= 25 and _mean(volumes[-25:-1]) > 0 else 1.0
        )

        trend_score = 20 if last > ma20 > ma50 else 13 if last > ma20 else 5
        momentum_score = _score_change(change_24h, 0.8)
        short_score = _score_change(change_6h, 1.2)
        liquidity_score = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
        stability_score = max(0, min(20, round(20 - hourly_volatility * 2)))
        volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
        score = min(100, trend_score + momentum_score + short_score +
                    liquidity_score + stability_score + volume_score)

        if score >= 75 and last > ma20 and change_6h > 0:
            signal = "BUY"
        elif score >= 55:
            signal = "WATCH"
        else:
            signal = "HOLD"

        risk = "Высокий" if hourly_volatility >= 2.5 else (
            "Средний" if hourly_volatility >= 1.2 else "Умеренный"
        )

        return {
            "symbol": symbol,
            "quote_currency": pair_info.get("quoteAsset", "USDT"),
            "exchange": "Binance Spot",
            "pair_status": pair_info.get("status"),
            "exchange_pair_verified": pair_verified,
            "trade_ready": pair_verified,
            "price": last,
            "change_24h_pct": round(float(ticker.get("priceChangePercent", 0)), 2),
            "momentum_6h_pct": round(change_6h, 2),
            "momentum_24h_pct": round(change_24h, 2),
            "quote_volume_24h": round(quote_volume, 2),
            "volume_ratio": round(volume_ratio, 2),
            "ma20": round(ma20, 8),
            "ma50": round(ma50, 8),
            "hourly_volatility_pct": round(hourly_volatility, 3),
            "score": score,
            "signal": signal,
            "risk": risk,
        }
    except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


async def _scan_coingecko(client: httpx.AsyncClient, limit: int, min_volume: float):
    response = await client.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "volume_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h,7d,30d",
        },
        headers={"accept": "application/json"},
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("CoinGecko returned an unexpected response")

    assets = []
    for coin in rows:
        if not isinstance(coin, dict):
            continue
        symbol = str(coin.get("symbol", "")).upper()
        volume = float(coin.get("total_volume") or 0)
        price = coin.get("current_price")
        if not symbol or price is None or volume < min_volume:
            continue
        if symbol in {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"}:
            continue

        change_24h = float(coin.get("price_change_percentage_24h") or 0)
        change_7d = float(coin.get("price_change_percentage_7d_in_currency") or 0)
        change_30d = float(coin.get("price_change_percentage_30d_in_currency") or 0)

        trend_score = 25 if change_7d > 0 and change_30d > 0 else (
            15 if change_7d > 0 else 5
        )
        momentum_score = max(0, min(25, round(12.5 + change_24h * 1.5)))
        medium_score = max(0, min(25, round(12.5 + change_7d * 0.5)))
        liquidity_score = min(25, max(0, round(math.log10(max(volume, 1)) * 2)))
        score = trend_score + momentum_score + medium_score + liquidity_score

        assets.append({
            "symbol": symbol,
            "quote_currency": "USD",
            "exchange_pair_verified": False,
            "trade_ready": False,
            "name": coin.get("name"),
            "price": float(price),
            "change_24h_pct": round(change_24h, 2),
            "momentum_6h_pct": None,
            "momentum_24h_pct": round(change_24h, 2),
            "quote_volume_24h": round(volume, 2),
            "volume_ratio": None,
            "ma20": None,
            "ma50": None,
            "hourly_volatility_pct": None,
            "score": score,
            "signal": "WATCH" if score >= 60 else "HOLD",
            "risk": "Не рассчитан: резервный источник",
            "market_cap_rank": coin.get("market_cap_rank"),
        })

    assets.sort(key=lambda item: item["score"], reverse=True)
    return {
        "source": "CoinGecko public market data (резервный режим)",
        "scanned": min(len(assets), limit),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets[:limit],
        "notice": (
            "Резервная оценка ограничена: нет часовых свечей, MA20/MA50 и оценки "
            "волатильности. Сигналы BUY не выдаются. Оценка не гарантирует прибыль; "
            "перед сделкой проверьте доступность актива на выбранной бирже."
        ),
    }


async def _scan_coinpaprika(client: httpx.AsyncClient, limit: int, min_volume: float):
    response = await client.get(
        "https://api.coinpaprika.com/v1/tickers",
        params={"quotes": "USD"},
        headers={"accept": "application/json"},
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("CoinPaprika returned an unexpected response")

    assets = []
    for coin in rows:
        if not isinstance(coin, dict):
            continue
        quote = (coin.get("quotes") or {}).get("USD") or {}
        symbol = str(coin.get("symbol") or "").upper()
        name = str(coin.get("name") or "")
        normalized_name = name.lower()
        price = quote.get("price")
        volume = float(quote.get("volume_24h") or 0)
        rank = int(coin.get("rank") or 0)
        if not symbol or price is None or volume < min_volume or rank <= 0:
            continue
        if symbol in {
            "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE",
            "USDS", "USD1", "USDG", "RLUSD", "USDP", "PYUSD",
            "GUSD", "FRAX", "USDD", "UST", "USTC", "USDX",
            "U", "USDJ", "EURC", "EURS",
        }:
            continue
        # Exclude tokenized equities and securities from the crypto-only scanner.
        if any(term in normalized_name for term in (
            "tokenized stock", "xstock", "backpack securities",
            "tokenized equity", "wrapped stock",
        )):
            continue

        change_6h = float(quote.get("percent_change_6h") or 0)
        change_24h = float(quote.get("percent_change_24h") or 0)
        change_7d = float(quote.get("percent_change_7d") or 0)
        trend_score = 25 if change_7d > 0 and change_24h > 0 else (
            15 if change_7d > 0 else 5
        )
        momentum_score = max(0, min(25, round(12.5 + change_24h * 1.5)))
        short_score = max(0, min(25, round(12.5 + change_6h)))
        liquidity_score = min(25, max(0, round(math.log10(max(volume, 1)) * 2)))
        score = trend_score + momentum_score + short_score + liquidity_score

        assets.append({
            "symbol": symbol,
            "quote_currency": "USD",
            "exchange_pair_verified": False,
            "trade_ready": False,
            "name": coin.get("name"),
            "price": float(price),
            "change_24h_pct": round(change_24h, 2),
            "momentum_6h_pct": round(change_6h, 2),
            "momentum_24h_pct": round(change_24h, 2),
            "quote_volume_24h": round(volume, 2),
            "volume_ratio": None,
            "ma20": None,
            "ma50": None,
            "hourly_volatility_pct": None,
            "score": score,
            "signal": "WATCH" if score >= 60 else "HOLD",
            "risk": "Не рассчитан: резервный источник",
            "market_cap_rank": rank,
            "data_updated_at": coin.get("last_updated"),
        })

    assets.sort(key=lambda item: item["score"], reverse=True)
    return {
        "source": "CoinPaprika public market data (резервный режим)",
        "scanned": min(len(assets), limit),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets[:limit],
        "notice": (
            "Резервная оценка ограничена: нет часовых свечей и расчёта MA20/MA50 "
            "и волатильности. Сигналы BUY не выдаются. Цены и объёмы CoinPaprika "
            "агрегированы по рынкам в USD и не являются котировкой пары USDT на бирже. "
            "Перед сделкой отдельно проверьте торговую пару и ликвидность на выбранной бирже. "
            "Источник: CoinPaprika."
        ),
    }


@router.get("/api/v1/crypto/scan")
async def scan_crypto(
    limit: int = Query(30, ge=5, le=50),
    min_volume: float = Query(1_000_000, ge=0),
):
    cache_key = f"crypto:{limit}:{min_volume}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            exchange_info = await _get_json(client, "/api/v3/exchangeInfo")
            if not isinstance(exchange_info, dict) or not isinstance(exchange_info.get("symbols"), list):
                raise HTTPException(status_code=503, detail="Binance вернула неожиданный список торговых пар.")

            tickers = await _get_json(client, "/api/v3/ticker/24hr")
            if not isinstance(tickers, list):
                raise HTTPException(status_code=503, detail="Binance вернула неожиданный список рыночных котировок.")

            spot_pairs = {
                item.get("symbol"): item
                for item in exchange_info["symbols"]
                if isinstance(item, dict)
                and item.get("symbol")
                and item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed") is True
                and item.get("quoteAsset") == "USDT"
            }

            excluded_base_assets = {
                "USDT", "USDC", "USD1", "RLUSD", "DAI", "FDUSD", "TUSD",
                "USDE", "USDS", "USDP", "PYUSD", "GUSD", "FRAX", "USDD",
                "UST", "USTC", "USDX", "USDJ", "EURC", "EURS"
            }
            eligible = [
                item for item in tickers
                if isinstance(item, dict)
                and item.get("symbol", "").endswith("USDT")
                and not item.get("symbol", "").endswith(EXCLUDED_SUFFIXES)
                and item.get("symbol", "") in spot_pairs
                and item.get("symbol", "")[:-4] not in excluded_base_assets
                and item.get("symbol", "")[:-4] not in EXCLUDED_NON_CRYPTO_BASE_ASSETS
                and float(item.get("quoteVolume", 0) or 0) >= min_volume
            ]
            eligible.sort(key=lambda item: float(item.get("quoteVolume", 0)), reverse=True)
            selected = eligible[:limit]
            results = await asyncio.gather(
                *[_analyze_symbol(client, item, spot_pairs[item.get("symbol", "")]) for item in selected]
            )
    except BinanceCooldownError as exc:
        logger.warning("Binance cooldown active; switching to CoinGecko fallback")
        try:
            async with httpx.AsyncClient(timeout=20) as fallback_client:
                payload = await _scan_coingecko(fallback_client, limit, min_volume)
            _cache[cache_key] = (time.time(), payload)
            return payload
        except (httpx.HTTPError, ValueError, TypeError) as fallback_exc:
            logger.warning("CoinGecko fallback failed: %s", type(fallback_exc).__name__)
            try:
                async with httpx.AsyncClient(timeout=20) as third_client:
                    payload = await _scan_coinpaprika(third_client, limit, min_volume)
                _cache[cache_key] = (time.time(), payload)
                return payload
            except (httpx.HTTPError, ValueError, TypeError) as third_exc:
                logger.exception("CoinPaprika fallback failed: %s", type(third_exc).__name__)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Binance и CoinGecko временно ограничили запросы; "
                        "резервный источник CoinPaprika сейчас недоступен."
                    ),
                    headers={"Retry-After": str(exc.retry_after)},
                ) from third_exc
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.exception("Binance market-data request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Криптовалютные данные Binance временно недоступны.",
        ) from exc

    assets = [item for item in results if item is not None]
    assets.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "source": "Binance public market data",
        "scanned": len(assets),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets,
        "notice": (
            "Оценка является аналитическим индикатором, а не гарантией прибыли. "
            "Не учитывает комиссии, проскальзывание и индивидуальные ограничения."
        ),
    }
    _cache[cache_key] = (time.time(), payload)
    return payload
