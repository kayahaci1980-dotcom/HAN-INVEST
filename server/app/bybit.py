import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
BASE = "https://api.bybit.com"
CACHE_TTL = 45
_NAME_CACHE_TTL = 21600
_name_cache: tuple[float, dict[str, str]] = (0.0, {})
_cache: tuple[float, dict[str, Any]] | None = None


async def get_coin_names(client: httpx.AsyncClient) -> dict[str, str]:
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
        return _name_cache[1]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def calculate_position_sizing(
    entry_price: float, stop_loss: float, take_profit: float,
    capital_usdt: float, risk_pct: float, fee_rate: float,
) -> dict[str, float | str | bool]:
    risk_per_coin = (entry_price - stop_loss) + (entry_price + stop_loss) * fee_rate
    if not all(math.isfinite(x) for x in (entry_price, stop_loss, take_profit, capital_usdt, risk_pct)):
        return {"available": False, "reason": "Некорректные числовые данные"}
    if risk_per_coin <= 0 or entry_price <= 0 or capital_usdt <= 0:
        return {"available": False, "reason": "Некорректные уровни сделки"}
    risk_budget = capital_usdt * risk_pct / 100
    quantity_by_risk = risk_budget / risk_per_coin
    quantity_by_capital = capital_usdt / (entry_price * (1 + fee_rate))
    quantity = min(quantity_by_risk, quantity_by_capital)
    position_value = quantity * entry_price
    entry_fee = position_value * fee_rate
    stop_value = quantity * stop_loss
    stop_fee = stop_value * fee_rate
    actual_loss = position_value - stop_value + entry_fee + stop_fee
    target_value = quantity * take_profit
    target_fee = target_value * fee_rate
    net_profit = target_value - position_value - entry_fee - target_fee
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


async def get_json(client: httpx.AsyncClient, path: str, params: dict):
    response = await client.get(f"{BASE}{path}", params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("retCode") != 0:
        raise ValueError(str(data.get("retMsg", "Bybit API error")))
    return data.get("result", {})


async def analyze(
    client: httpx.AsyncClient, ticker: dict, instrument: dict, btc_state: str,
    capital_usdt: float | None, risk_pct: float, fee_rate: float,
    btc_change_24h_pct: float = 0.0,
):
    symbol = ticker.get("symbol", "")
    try:
        result = await get_json(client, "/v5/market/kline", {
            "category": "spot", "symbol": symbol, "interval": "60", "limit": "60"
        })
        rows = list(reversed(result.get("list", [])))[:-1]
        if len(rows) < 50:
            return None
        closes = [float(row[4]) for row in rows]
        highs = [float(row[2]) for row in rows]
        lows = [float(row[3]) for row in rows]
        volumes = [float(row[5]) for row in rows]
        if not all(math.isfinite(x) for x in closes + highs + lows + volumes):
            return None
        price = float(ticker.get("lastPrice") or closes[-1])
        if not math.isfinite(price) or price <= 0:
            return None

        ma20, ma50 = mean(closes[-20:]), mean(closes[-50:])
        change6 = (closes[-1] / closes[-7] - 1) * 100 if closes[-7] else 0.0
        change24_candles = (closes[-1] / closes[-25] - 1) * 100 if closes[-25] else 0.0
        change24 = float(ticker.get("price24hPcnt") or 0) * 100
        quote_volume = float(ticker.get("turnover24h") or 0)
        baseline_volume = mean(volumes[-25:-1])
        volume_ratio = volumes[-1] / baseline_volume if baseline_volume > 0 else 0.0
        true_ranges = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            for i in range(1, len(closes))
        ]
        # Use 14 completed DAILY candles for ATR-based trade levels.
        daily_result = await get_json(client, "/v5/market/kline", {
            "category": "spot", "symbol": symbol, "interval": "D", "limit": "20"
        })
        daily_rows = list(reversed(daily_result.get("list", [])))[:-1]
        daily_closes = [float(row[4]) for row in daily_rows]
        daily_highs = [float(row[2]) for row in daily_rows]
        daily_lows = [float(row[3]) for row in daily_rows]
        if len(daily_closes) < 15:
            return None
        daily_true_ranges = [
            max(daily_highs[i] - daily_lows[i],
                abs(daily_highs[i] - daily_closes[i - 1]),
                abs(daily_lows[i] - daily_closes[i - 1]))
            for i in range(1, len(daily_closes))
        ]
        atr_14_1h = mean(true_ranges[-14:])
        atr = mean(daily_true_ranges[-14:])
        if not math.isfinite(atr) or atr <= 0:
            return None
        returns = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes)) if closes[i-1] > 0]
        hourly_volatility = math.sqrt(mean([x*x for x in returns])) * 100

        trend_score = 20 if closes[-1] > ma20 > ma50 else 13 if closes[-1] > ma20 else 5
        momentum_score = max(0, min(20, round(10 + change24_candles * 0.8)))
        short_score = max(0, min(20, round(10 + change6 * 1.2)))
        liquidity_score = min(20, max(0, round(math.log10(max(quote_volume, 1)) * 2)))
        stability_score = max(0, min(20, round(20 - hourly_volatility * 2)))
        volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
        score = min(100, trend_score + momentum_score + short_score + liquidity_score + stability_score + volume_score)

        volume_confirmed = volume_ratio >= 1.2
        strong_uptrend = price > ma20 > ma50
        pair_verified = instrument.get("status") == "Trading" and instrument.get("quoteCoin") == "USDT"
        setup = (
            pair_verified and score >= 80 and strong_uptrend and change6 >= 1.0
            and volume_confirmed and quote_volume >= 1_000_000
            and (change24 - btc_change_24h_pct) > 0
            and atr > 0 and math.isfinite(atr)
        )
        signal = "BUY" if setup and btc_state == "BULLISH" else "WATCH" if score >= 55 else "HOLD"
        early = "EARLY BUY" if setup and btc_state == "SIDEWAYS" else None

        entry = price if signal == "BUY" else None
        stop = price - 1.5 * atr if signal == "BUY" else None
        target = price + 3.0 * atr if signal == "BUY" else None
        sizing = (
            calculate_position_sizing(entry, stop, target, capital_usdt, risk_pct, fee_rate)
            if signal == "BUY" and capital_usdt is not None else None
        )

        return {
            "symbol": symbol, "name": symbol[:-4], "exchange": "Bybit Spot",
            "source": "bybit", "price": price, "change_24h_pct": round(change24, 2),
            "momentum_6h_pct": round(change6, 2), "momentum_24h_pct": round(change24_candles, 2),
            "relative_strength_vs_btc_24h_pct": round(change24 - btc_change_24h_pct, 2),
            "quote_volume_24h": round(quote_volume, 2),
            "volume_ratio": round(volume_ratio, 2), "volume_confirmed": volume_confirmed,
            "ma20": round(ma20, 8), "ma50": round(ma50, 8), "atr_14": round(atr, 8),
            "atr_14_1d": round(atr, 8), "atr_14_1h": round(atr_14_1h, 8),
            "atr_timeframe": "1D",
            "hourly_volatility_pct": round(hourly_volatility, 3),
            "entry_price": round(entry, 8) if entry is not None else None,
            "stop_loss": round(stop, 8) if stop is not None else None,
            "take_profit": round(target, 8) if target is not None else None,
            "risk_reward": "1:2" if signal == "BUY" else None,
            "position_sizing": sizing,
            "score": score, "signal": signal, "early_signal": early,
            "trade_ready": pair_verified, "pair_status": instrument.get("status"),
            "quote_currency": "USDT",
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return None


@router.get("/api/v1/crypto/bybit/scan")
async def scan_bybit(
    limit: int = Query(500, ge=1, le=500),
    min_volume: float = Query(1_000_000, ge=0),
    capital_usdt: float | None = Query(None, gt=0),
    risk_pct: float = Query(1.0, ge=0.1, le=5.0),
    fee_rate: float = Query(0.001, ge=0, le=0.01),
):
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL and _cache[1].get("cache_key") == (limit, min_volume, capital_usdt, risk_pct, fee_rate):
        return _cache[1]
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
            instruments_data, tickers_data = await asyncio.gather(
                get_json(client, "/v5/market/instruments-info", {"category": "spot", "limit": "1000"}),
                get_json(client, "/v5/market/tickers", {"category": "spot"}),
            )
            instruments = {
                x.get("symbol"): x for x in instruments_data.get("list", [])
                if x.get("quoteCoin") == "USDT" and x.get("status") == "Trading"
            }
            tickers = [
                x for x in tickers_data.get("list", [])
                if x.get("symbol") in instruments and float(x.get("turnover24h") or 0) >= min_volume
            ]
            btc = next((x for x in tickers if x.get("symbol") == "BTCUSDT"), None)
            btc_change_24h_pct = float(btc.get("price24hPcnt") or 0) * 100 if btc else 0.0
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
                    btc_info = {
                        "available": True, "buy_allowed": btc_state == "BULLISH",
                        "market_state": btc_state, "price": price,
                        "ma20": round(ma20, 2), "ma50": round(ma50, 2),
                    }
            selected = sorted(tickers, key=lambda x: float(x.get("turnover24h") or 0), reverse=True)[:limit]
            semaphore = asyncio.Semaphore(5)

            async def limited(item):
                async with semaphore:
                    return await analyze(
                        client, item, instruments[item["symbol"]], btc_state,
                        capital_usdt, risk_pct, fee_rate, btc_change_24h_pct,
                    )

            assets = [x for x in await asyncio.gather(*(limited(x) for x in selected)) if x]
            coin_names = await get_coin_names(client)
            for asset in assets:
                base_symbol = str(asset.get("symbol") or "").removesuffix("USDT")
                asset["name"] = coin_names.get(base_symbol, base_symbol)
            assets.sort(key=lambda x: x["score"], reverse=True)
            for rank, asset in enumerate(assets, 1):
                asset["market_rank"] = rank
            payload = {
                "source": "Bybit public market data", "exchange": "Bybit Spot",
                "scanned": len(assets), "eligible_pairs": len(instruments),
                "btc_market_filter": btc_info, "updated_at": datetime.now(timezone.utc).isoformat(),
                "assets": assets, "limit": limit,
                "cache_key": (limit, min_volume, capital_usdt, risk_pct, fee_rate),
                "notice": (
                    "Только спотовые пары Bybit USDT. MA20, MA50 и объём рассчитаны по закрытым часовым свечам; ATR(14) для Stop Loss и Take Profit — по 14 завершённым дневным свечам (1D). "
                    "BUY требует подтверждённой пары, BULLISH BTC и всех фильтров актива. "
                    "Размер позиции рассчитывается по риску от капитала с учётом оценочной комиссии."
                ),
            }
            _cache = (time.time(), payload)
            return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Данные Bybit Spot временно недоступны.") from exc


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes[-period:]]
    losses = [max(-x, 0.0) for x in changes[-period:]]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


async def analyze_short(
    client: httpx.AsyncClient, ticker: dict, instrument: dict,
    btc_change_24h_pct: float,
) -> dict[str, Any] | None:
    """Independent bearish score for Bybit USDT perpetual contracts."""
    symbol = str(ticker.get("symbol") or "")
    try:
        result = await get_json(client, "/v5/market/kline", {
            "category": "linear", "symbol": symbol, "interval": "60", "limit": "60"
        })
        rows = list(reversed(result.get("list", [])))[:-1]
        if len(rows) < 50:
            return None
        closes = [float(row[4]) for row in rows]
        volumes = [float(row[5]) for row in rows]
        if not all(math.isfinite(x) for x in closes + volumes):
            return None
        price = float(ticker.get("lastPrice") or closes[-1])
        if not math.isfinite(price) or price <= 0:
            return None
        ma20, ma50 = mean(closes[-20:]), mean(closes[-50:])
        change6 = (closes[-1] / closes[-7] - 1.0) * 100 if closes[-7] else 0.0
        change24 = float(ticker.get("price24hPcnt") or 0) * 100
        quote_volume = float(ticker.get("turnover24h") or 0)
        baseline_volume = mean(volumes[-25:-1])
        volume_ratio = volumes[-1] / baseline_volume if baseline_volume > 0 else 0.0
        rsi = _rsi(closes)
        relative_weakness = change24 - btc_change_24h_pct

        trend = 25 if price < ma20 < ma50 else 17 if price < ma20 else 5
        momentum = max(0, min(20, round(10 - change6 * 1.2)))
        daily_momentum = max(0, min(15, round(7.5 - change24 * 0.5)))
        rsi_score = 15 if rsi < 40 else 10 if rsi < 48 else 5 if rsi < 55 else 0
        volume_score = 10 if volume_ratio >= 1.2 else 5 if volume_ratio >= 0.8 else 0
        liquidity_score = min(15, max(0, round(math.log10(max(quote_volume, 1)) * 1.5)))
        weakness_score = 10 if relative_weakness <= -2 else 6 if relative_weakness < 0 else 0
        short_score = min(100, trend + momentum + daily_momentum + rsi_score
                          + volume_score + liquidity_score + weakness_score)
        pair_verified = (
            instrument.get("status") == "Trading"
            and instrument.get("quoteCoin") == "USDT"
            and instrument.get("contractType") in ("LinearPerpetual", "LinearFutures")
        )
        short_ready = (
            pair_verified and short_score >= 65 and price < ma20 < ma50
            and rsi < 50 and volume_ratio >= 0.8 and quote_volume >= 1_000_000
        )
        return {
            "symbol": symbol, "exchange": "Bybit USDT Perpetual",
            "source": "bybit_linear", "side": "SHORT",
            "price": price, "change_24h_pct": round(change24, 2),
            "momentum_6h_pct": round(change6, 2),
            "relative_strength_vs_btc_24h_pct": round(relative_weakness, 2),
            "ma20": round(ma20, 8), "ma50": round(ma50, 8),
            "rsi_14": round(rsi, 2), "volume_ratio": round(volume_ratio, 2),
            "quote_volume_24h": round(quote_volume, 2),
            "short_score": short_score, "score": short_score,
            "signal": "SHORT" if short_ready else "WATCH",
            "short_ready": short_ready, "trade_ready": pair_verified,
            "pair_status": instrument.get("status"), "quote_currency": "USDT",
            "leverage": 1,
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return None


_short_cache: tuple[float, dict[str, Any]] | None = None


@router.get("/api/v1/crypto/bybit/short-scan")
async def scan_bybit_short(
    limit: int = Query(50, ge=1, le=100),
    min_volume: float = Query(1_000_000, ge=0),
):
    """Bearish-only scanner for virtual 1x USDT perpetual paper shorts."""
    global _short_cache
    if (_short_cache and time.time() - _short_cache[0] < CACHE_TTL
            and _short_cache[1].get("cache_key") == (limit, min_volume)):
        return _short_cache[1]
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "HAN-INVEST/1.0"}) as client:
            instruments_data, tickers_data = await asyncio.gather(
                get_json(client, "/v5/market/instruments-info", {"category": "linear", "limit": "1000"}),
                get_json(client, "/v5/market/tickers", {"category": "linear"}),
            )
            instruments = {
                x.get("symbol"): x for x in instruments_data.get("list", [])
                if x.get("quoteCoin") == "USDT"
                and x.get("status") == "Trading"
                and x.get("contractType") in ("LinearPerpetual", "LinearFutures")
            }
            tickers = [
                x for x in tickers_data.get("list", [])
                if x.get("symbol") in instruments
                and float(x.get("turnover24h") or 0) >= min_volume
            ]
            btc = next((x for x in tickers if x.get("symbol") == "BTCUSDT"), None)
            btc_change = float(btc.get("price24hPcnt") or 0) * 100 if btc else 0.0
            selected = sorted(tickers, key=lambda x: float(x.get("turnover24h") or 0), reverse=True)[:limit]
            semaphore = asyncio.Semaphore(5)

            async def limited(item):
                async with semaphore:
                    return await analyze_short(client, item, instruments[item["symbol"]], btc_change)

            assets = [x for x in await asyncio.gather(*(limited(x) for x in selected)) if x]
            assets.sort(key=lambda x: x["short_score"], reverse=True)
            for rank, asset in enumerate(assets, 1):
                asset["market_rank"] = rank
            payload = {
                "source": "Bybit public linear perpetual market data",
                "exchange": "Bybit USDT Perpetual", "side": "SHORT",
                "scanned": len(assets), "eligible_pairs": len(instruments),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "assets": assets, "limit": limit,
                "cache_key": (limit, min_volume),
                "notice": "Отдельная медвежья оценка SHORT Score 0–100. Виртуальная торговля 1x; реальные ордера не отправляются.",
            }
            _short_cache = (time.time(), payload)
            return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Данные Bybit USDT Perpetual временно недоступны.") from exc
