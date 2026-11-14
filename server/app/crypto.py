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
_NAME_CACHE_TTL = 21600
_name_cache: tuple[float, dict[str, str]] = (0.0, {})
_cache: dict[str, tuple[float, Any]] = {}
_semaphore = asyncio.Semaphore(2)
_blocked_until = 0.0


async def _get_coin_names(client: httpx.AsyncClient) -> dict[str, str]:
    global _name_cache
    if time.time() - _name_cache[0] < _NAME_CACHE_TTL and _name_cache[1]:
        return _name_cache[1]
    try:
        response = await client.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1, "sparkline": "false"},
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        names = {}
        for coin in response.json():
            symbol = str(coin.get("symbol") or "").upper()
            name = str(coin.get("name") or "").strip()
            if symbol and name:
                names.setdefault(symbol, name)
        _name_cache = (time.time(), names)
        return names
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("Coin names lookup unavailable; showing ticker symbols")
        return _name_cache[1]


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
    "U", "EUR", "XAUT", "TQQQB",
    "QQQB", "MSTRB", "PAXG", "CRCLB", "SKHYB", "SNXXB",
    # Newly observed stablecoins and tokenized-equity symbols from live scans.
    "BFUSD", "RWUSD", "XUSD", "AMDB", "BMNRB", "NBISB",
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


def _calculate_position_sizing(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    capital_usdt: float,
    risk_pct: float,
    fee_rate: float,
) -> dict[str, float | str | bool]:
    # Include estimated entry and exit taker fees in risk and reward.
    risk_per_coin = (entry_price - stop_loss) + (entry_price + stop_loss) * fee_rate
    if risk_per_coin <= 0 or entry_price <= 0:
        return {"available": False, "reason": "Некорректные уровни сделки"}

    risk_budget = capital_usdt * risk_pct / 100
    quantity_by_risk = risk_budget / risk_per_coin
    quantity_by_capital = capital_usdt / (entry_price * (1 + fee_rate))
    quantity = min(quantity_by_risk, quantity_by_capital)
    position_value = quantity * entry_price
    entry_fee = position_value * fee_rate
    exit_value_at_stop = quantity * stop_loss
    stop_fee = exit_value_at_stop * fee_rate
    actual_loss = position_value - exit_value_at_stop + entry_fee + stop_fee
    exit_value_at_target = quantity * take_profit
    target_fee = exit_value_at_target * fee_rate
    net_profit = exit_value_at_target - position_value - entry_fee - target_fee

    return {
        "available": True,
        "risk_pct": round(risk_pct, 3),
        "risk_budget_usdt": round(risk_budget, 4),
        "quantity": round(quantity, 8),
        "position_value_usdt": round(position_value, 4),
        "entry_fee_estimate_usdt": round(entry_fee, 4),
        "potential_loss_at_stop_usdt": round(actual_loss, 4),
        "potential_profit_at_target_usdt": round(net_profit, 4),
        "fee_rate_per_side_pct": round(fee_rate * 100, 4),
        "capital_limited": quantity_by_capital < quantity_by_risk,
        "leverage": "spot_only",
    }


async def _analyze_symbol(client: httpx.AsyncClient, ticker: dict, pair_info: dict,
                          capital_usdt: float | None = None, risk_pct: float = 1.0,
                          fee_rate: float = 0.001, btc_market_state: str = "UNKNOWN",
                          btc_change_24h_pct: float = 0.0):
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
        # Binance's final kline is the currently forming hour. Exclude it so
        # indicators and volume confirmation use completed candles only.
        closed_candles = candles[:-1]
        closes = [float(row[4]) for row in closed_candles if len(row) > 5]
        highs = [float(row[2]) for row in closed_candles if len(row) > 5]
        lows = [float(row[3]) for row in closed_candles if len(row) > 5]
        volumes = [float(row[5]) for row in closed_candles if len(row) > 5]
        if len(closes) < 50 or len(highs) != len(closes) or len(lows) != len(closes):
            return None

        last = closes[-1]
        current_price = float(ticker.get("lastPrice") or last)
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
        true_ranges = [
            max(highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))
        ]
        # ATR(14) for trade levels is calculated from 14 completed DAILY candles.
        daily_candles = await _get_json(
            client,
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1d", "limit": 20},
        )
        daily_closed = daily_candles[:-1]
        daily_closes = [float(row[4]) for row in daily_closed if len(row) > 5]
        daily_highs = [float(row[2]) for row in daily_closed if len(row) > 5]
        daily_lows = [float(row[3]) for row in daily_closed if len(row) > 5]
        if len(daily_closes) < 15 or len(daily_highs) != len(daily_closes) or len(daily_lows) != len(daily_closes):
            return None
        daily_true_ranges = [
            max(daily_highs[i] - daily_lows[i],
                abs(daily_highs[i] - daily_closes[i - 1]),
                abs(daily_lows[i] - daily_closes[i - 1]))
            for i in range(1, len(daily_closes))
        ]
        atr_14_1h = _mean(true_ranges[-14:])
        atr_14 = _mean(daily_true_ranges[-14:])
        if not math.isfinite(atr_14) or atr_14 <= 0:
            return None

        trend_score = 20 if last > ma20 > ma50 else 13 if last > ma20 else 5
        momentum_score = _score_change(change_24h, 0.8)
        short_score = _score_change(change_6h, 1.2)
        liquidity_score = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
        stability_score = max(0, min(20, round(20 - hourly_volatility * 2)))
        volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
        score = min(100, trend_score + momentum_score + short_score +
                    liquidity_score + stability_score + volume_score)

        # BUY is deliberately selective: require a strong score, aligned MA trend,
        # positive short-term momentum, and a meaningful closed-candle volume surge.
        volume_confirmed = volume_ratio >= 1.2
        # Use the live ticker price too: a stale candle close must not keep BUY active.
        strong_uptrend = current_price > ma20 > ma50
        asset_setup_confirmed = (
            score >= 83 and strong_uptrend and change_6h >= 1.0
            and volume_confirmed and quote_volume >= 1_000_000
            and (change_24h - btc_change_24h_pct) > 0
        )
        early_buy = asset_setup_confirmed and btc_market_state == "SIDEWAYS"
        if asset_setup_confirmed and btc_market_state == "BULLISH":
            signal = "BUY"
        elif score >= 55:
            signal = "WATCH"
        else:
            signal = "HOLD"

        risk = "Высокий" if hourly_volatility >= 2.5 else (
            "Средний" if hourly_volatility >= 1.2 else "Умеренный"
        )

        entry_price = current_price if signal == "BUY" else None
        stop_loss = current_price - 1.5 * atr_14 if signal == "BUY" else None
        take_profit = current_price + 3.0 * atr_14 if signal == "BUY" else None
        position_sizing = (
            _calculate_position_sizing(
                entry_price, stop_loss, take_profit, capital_usdt, risk_pct, fee_rate
            )
            if signal == "BUY" and capital_usdt is not None else None
        )

        return {
            "symbol": symbol,
            "quote_currency": pair_info.get("quoteAsset", "USDT"),
            "exchange": "Binance Spot",
            "pair_status": pair_info.get("status"),
            "exchange_pair_verified": pair_verified,
            "trade_ready": pair_verified,
            "price": current_price,
            "change_24h_pct": round(float(ticker.get("priceChangePercent", 0)), 2),
            "momentum_6h_pct": round(change_6h, 2),
            "momentum_24h_pct": round(change_24h, 2),
            "relative_strength_vs_btc_24h_pct": round(change_24h - btc_change_24h_pct, 2),
            "quote_volume_24h": round(quote_volume, 2),
            "volume_ratio": round(volume_ratio, 2),
            "volume_confirmed": volume_confirmed,
            "atr_14": round(atr_14, 8),
            "atr_14_1d": round(atr_14, 8),
            "atr_14_1h": round(atr_14_1h, 8),
            "atr_timeframe": "1D",
            "entry_price": round(entry_price, 8) if entry_price is not None else None,
            "stop_loss": round(stop_loss, 8) if stop_loss is not None else None,
            "take_profit": round(take_profit, 8) if take_profit is not None else None,
            "risk_reward": "1:2" if signal == "BUY" else None,
            "position_sizing": position_sizing,
            "ma20": round(ma20, 8),
            "ma50": round(ma50, 8),
            "hourly_volatility_pct": round(hourly_volatility, 3),
            "score": score,
            "signal": signal,
            "early_signal": "EARLY BUY" if early_buy else None,
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
    limit: int = Query(30, ge=5, le=500),
    universe_limit: int = Query(100, ge=30, le=500),
    min_volume: float = Query(1_000_000, ge=0),
    capital_usdt: float | None = Query(None, gt=0),
    risk_pct: float = Query(1.0, ge=0.1, le=5.0),
    fee_rate: float = Query(0.001, ge=0, le=0.01),
):
    cache_key = f"crypto:{limit}:{universe_limit}:{min_volume}:{capital_usdt}:{risk_pct}:{fee_rate}"
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
                and item.get("symbol", "")[:-4].isascii()
                and item.get("symbol", "")[:-4].isalnum()
                and item.get("symbol", "")[:-4] not in excluded_base_assets
                and item.get("symbol", "")[:-4] not in EXCLUDED_NON_CRYPTO_BASE_ASSETS
                and float(item.get("quoteVolume", 0) or 0) >= min_volume
            ]
            eligible.sort(key=lambda item: float(item.get("quoteVolume", 0)), reverse=True)
            # Analyze a wider liquid USDT spot universe, then return only the top-ranked shortlist.
            # Broad-market safety gate: only allow BUY when BTC is above its hourly MA20
            # and MA20 is above MA50, using a live ticker price plus closed candles.
            btc_market = {"available": False, "buy_allowed": False, "reason": "Нет данных BTC"}
            btc_ticker = next((item for item in tickers
                               if isinstance(item, dict) and item.get("symbol") == "BTCUSDT"), None)
            btc_change_24h_pct = float(btc_ticker.get("priceChangePercent") or 0) if btc_ticker else 0.0
            if btc_ticker is not None and "BTCUSDT" in spot_pairs:
                try:
                    btc_candles = await _get_json(
                        client, "/api/v3/klines",
                        {"symbol": "BTCUSDT", "interval": "1h", "limit": 60},
                    )
                    btc_closed = btc_candles[:-1]
                    btc_closes = [float(row[4]) for row in btc_closed if len(row) > 5]
                    if len(btc_closes) >= 50:
                        btc_ma20 = _mean(btc_closes[-20:])
                        btc_ma50 = _mean(btc_closes[-50:])
                        btc_price = float(btc_ticker.get("lastPrice") or btc_closes[-1])
                        btc_ok = btc_price > btc_ma20 > btc_ma50
                        if btc_ok:
                            market_state = "BULLISH"
                            market_state_label = "Восходящий тренд"
                            market_reason = "BTC выше MA20, MA20 выше MA50"
                        elif btc_price < btc_ma20 < btc_ma50:
                            market_state = "BEARISH"
                            market_state_label = "Нисходящий тренд"
                            market_reason = "BTC ниже MA20, MA20 ниже MA50"
                        else:
                            market_state = "SIDEWAYS"
                            market_state_label = "Боковой или неопределённый рынок"
                            market_reason = "Направление BTC пока не подтверждено"
                        btc_market = {
                            "available": True,
                            "buy_allowed": btc_ok,
                            "market_state": market_state,
                            "market_state_label": market_state_label,
                            "price": btc_price,
                            "ma20": round(btc_ma20, 2),
                            "ma50": round(btc_ma50, 2),
                            "trend": market_state,
                            "reason": market_reason,
                        }
                except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError):
                    logger.warning("Could not calculate BTC market filter")
            selected = eligible[:universe_limit]
            results = await asyncio.gather(
                *[_analyze_symbol(client, item, spot_pairs[item.get("symbol", "")],
                                  capital_usdt, risk_pct, fee_rate,
                                  str(btc_market.get("market_state") or "UNKNOWN"),
                                  btc_change_24h_pct) for item in selected]
            )
            # Keep the HTTP client open while fetching optional display names.
            coin_names = await _get_coin_names(client)
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
    for asset in assets:
        base_symbol = str(asset.get("symbol") or "").removesuffix("USDT")
        asset["name"] = coin_names.get(base_symbol, base_symbol)
    assets.sort(key=lambda item: item["score"], reverse=True)
    for rank, asset in enumerate(assets, start=1):
        asset["market_rank"] = rank
    payload = {
        "source": "Binance public market data",
        "scanned": len(assets),
        "eligible_pairs": len(eligible),
        "universe_limit": universe_limit,
        "returned": min(len(assets), limit),
        "btc_market_filter": btc_market,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets[:limit],
        "notice": (
            f"Проверяются до {universe_limit} наиболее ликвидных активных спотовых пар USDT "
            f"из {len(eligible)} подходящих пар; в приложении показаны лучшие {min(len(assets), limit)} по рейтингу. "
            "Состояние рынка определяется по BTC: BULLISH (восходящий), BEARISH (нисходящий) или SIDEWAYS (боковой/неопределённый). При каждом запуске сканирования рынок и выбранные монеты проверяются заново. BUY разрешён только при BULLISH и выполнении всех фильтров актива; при BEARISH, SIDEWAYS или отсутствии данных BTC BUY блокируется. "
            "Для BUY Stop Loss и Take Profit рассчитаны по ATR(14) с отношением риска к прибыли 1:2. "
            "При передаче capital_usdt рассчитывается размер спотовой позиции с риском по умолчанию 1% "
            "от капитала; учитывается ориентировочная комиссия на вход и выход. "
            "Комиссия по умолчанию 0.1% за сторону и может отличаться от тарифа аккаунта. "
            "Расчёт не учитывает проскальзывание и не гарантирует исполнение по указанной цене."
        ),
    }
    _cache[cache_key] = (time.time(), payload)
    return payload
