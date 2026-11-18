import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ai/advisor", tags=["ai-advisor"])

_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT_SECONDS = 25
_MAX_RETRIES = 2
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Shared cooldown prevents market analysis and PAPER exit review from
# hammering Groq after a rate-limit response.
_GROQ_RATE_LIMIT_COOLDOWN_SECONDS = 180
_groq_cooldown_until: datetime | None = None


def _groq_cooldown_remaining(now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    if _groq_cooldown_until is None:
        return 0.0
    return max(0.0, (_groq_cooldown_until - current).total_seconds())


def _set_groq_rate_limit_cooldown(retry_after: str | None = None) -> float:
    global _groq_cooldown_until
    delay = max(_GROQ_RATE_LIMIT_COOLDOWN_SECONDS, _retry_delay_seconds(retry_after, 4))
    _groq_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return delay
_MAX_ASSETS = 12
_last_success_fingerprint: str | None = None
_last_success_result: dict[str, Any] | None = None
_latest: dict[str, Any] = {
    "available": False,
    "mode": "SHADOW",
    "provider": "Google Gemini",
    "model": os.getenv("GEMINI_MODEL", _DEFAULT_MODEL),
    "status": "NOT_CONFIGURED",
    "market_bias": "WAIT",
    "recommendations": [],
    "updated_at": None,
    "message": "Добавьте GEMINI_API_KEY в секреты сервера Render.",
}


def _candidate_payload(market: dict[str, Any]) -> list[dict[str, Any]]:
    assets = market.get("assets") or []
    if not isinstance(assets, list):
        return []
    # AI receives only Bybit pairs that already passed the app's directional score gate.
    eligible = []
    for item in assets:
        if not isinstance(item, dict) or not item.get("symbol") or item.get("trade_ready") is not True:
            continue
        try:
            if max(float(item.get("long_score") or 0), float(item.get("short_score") or 0)) < 85:
                continue
            volume = float(item.get("quote_volume_24h") or 0)
            if volume <= 0:
                continue
        except (TypeError, ValueError):
            continue
        eligible.append(item)
    selected = sorted(
        eligible,
        key=lambda item: float(item.get("quote_volume_24h") or 0),
        reverse=True,
    )[:_MAX_ASSETS]
    fields = (
        "symbol", "price", "change_1h_pct", "change_24h_pct",
        "momentum_6h_pct", "momentum_24h_pct",
        "relative_strength_vs_btc_24h_pct", "quote_volume_24h",
        "volume_ratio", "volume_confirmed", "ma20", "ma50",
        "atr_14_1h", "hourly_volatility_pct", "long_score",
        "short_score", "signal", "trade_ready",
    )
    return [
        {key: item.get(key) for key in fields if key in item}
        for item in selected
        if item.get("symbol")
    ]


def _extract_json(response: dict[str, Any]) -> dict[str, Any]:
    candidates = response.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates")
    parts = (
        candidates[0].get("content", {}).get("parts", [])
        if isinstance(candidates[0], dict) else []
    )
    text = "".join(
        str(part.get("text") or "")
        for part in parts if isinstance(part, dict)
    ).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini response must be a JSON object")
    return parsed


def _validated_result(raw: dict[str, Any], candidates: list[dict[str, Any]],
                      model: str, provider: str = "Google Gemini") -> dict[str, Any]:
    allowed = {str(item["symbol"]).upper() for item in candidates}
    decisions: dict[str, dict[str, Any]] = {}
    for item in raw.get("decisions", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        action = str(item.get("action") or "WAIT").upper()
        if symbol not in allowed or action not in {"LONG", "SHORT", "WAIT"}:
            continue
        try:
            confidence = max(0, min(100, int(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        decisions[symbol] = {
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "reason": str(item.get("reason") or "")[:240],
        }

    recommendations = [
        decisions.get(symbol, {
            "symbol": symbol,
            "action": "WAIT",
            "confidence": 0,
            "reason": f"{provider} не вернул проверенное решение для этой пары.",
        })
        for symbol in sorted(allowed)
    ]
    bias = str(raw.get("market_bias") or "WAIT").upper()
    if bias not in {"LONG", "SHORT", "MIXED", "WAIT"}:
        bias = "WAIT"
    return {
        "available": True,
        "mode": "SHADOW",
        "provider": provider,
        "model": model,
        "status": "OK",
        "market_bias": bias,
        "recommendations": recommendations,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": "Теневые рекомендации: сделки не открываются и не закрываются.",
    }


def _retry_delay_seconds(retry_after: str | None, attempt: int) -> float:
    """Return a bounded delay, preferring Google's Retry-After hint."""
    if retry_after:
        try:
            return min(15.0, max(0.5, float(retry_after)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return min(15.0, max(0.5, seconds))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(15.0, float(2 ** (attempt + 1)))


async def _gemini_fallback(candidates: list[dict[str, Any]], body: dict[str, Any],
                           api_key: str) -> dict[str, Any]:
    model = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    url = f"{_API_ROOT}/{model}:generateContent"
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers={"x-goog-api-key": api_key}, json=body)
        response.raise_for_status()
        parsed = _extract_json(response.json())
    return _validated_result(parsed, candidates, model, "Google Gemini (fallback)")


async def refresh_ai_advisor(market: dict[str, Any]) -> dict[str, Any]:
    """Ask Groq when configured, otherwise Gemini; advisory only."""
    global _latest, _last_success_fingerprint, _last_success_result
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    use_groq = bool(groq_key)
    provider = "Groq" if use_groq else "Google Gemini"
    api_key = groq_key if use_groq else gemini_key
    model = (
        os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
        if use_groq else
        (os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL)
    )
    if not api_key:
        _latest = {
            **_latest,
            "available": False,
            "mode": "SHADOW",
            "model": model,
            "status": "NOT_CONFIGURED",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": "Добавьте GROQ_API_KEY или GEMINI_API_KEY в секреты Render.",
        }
        return dict(_latest)

    candidates = _candidate_payload(market)
    if not candidates:
        _latest = {
            **_latest,
            "available": False,
            "model": model,
            "status": "NO_MARKET_DATA",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": "Нет подходящих рыночных данных для анализа.",
        }
        return dict(_latest)

    fingerprint_source = json.dumps(candidates, sort_keys=True, separators=(",", ":"), allow_nan=False)
    candidate_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    if candidate_fingerprint == _last_success_fingerprint and _last_success_result is not None:
        _latest = {**_last_success_result, "reused": True,
                   "message": "Использован сохранённый результат Groq: состав и показатели отобранных монет не изменились."}
        logger.info("AI analysis reused saved %s result; selected assets unchanged", provider)
        return dict(_latest)

    cooldown_remaining = _groq_cooldown_remaining()
    if use_groq and cooldown_remaining > 0:
        if gemini_key:
            try:
                _latest = await _gemini_fallback(candidates, body, gemini_key)
                _latest["message"] = "Groq временно ограничил запросы; использован резервный анализ Gemini."
                logger.info("Groq market analysis skipped during cooldown; Gemini fallback supplied advisory analysis")
                return dict(_latest)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as fallback_exc:
                logger.warning("Gemini fallback failed during Groq cooldown: %s", type(fallback_exc).__name__)
        _latest = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "RATE_LIMITED", "market_bias": "WAIT",
            "recommendations": [], "updated_at": datetime.now(timezone.utc).isoformat(),
            "retry_in_seconds": int(cooldown_remaining),
            "message": "Groq временно ограничил запросы; новые AI-входы ждут следующей проверки.",
        }
        return dict(_latest)

    prompt = {
        "task": (
            "Evaluate each supplied Bybit Spot USDT asset for a short-term "
            "directional setup. Return LONG, SHORT, or WAIT per asset. "
            "Use only supplied fields; do not invent news, prices, or indicators. "
            "Prefer WAIT when evidence conflicts or is insufficient. "
            "Confidence is a qualitative score from 0 to 100, not a probability "
            "or guarantee. Do not give leverage or position-size instructions."
        ),
        "btc_market_filter": market.get("btc_market_filter", {}),
        "assets": candidates,
    }
    body = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are the market-analysis component of HAN INVEST. "
                    "You only provide advisory signals. You cannot place orders. "
                    "Respect the supplied market data and return strict JSON."
                )
            }]
        },
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, separators=(",", ":"))}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "market_bias": {"type": "STRING", "enum": ["LONG", "SHORT", "MIXED", "WAIT"]},
                    "decisions": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "symbol": {"type": "STRING"},
                                "action": {"type": "STRING", "enum": ["LONG", "SHORT", "WAIT"]},
                                "confidence": {"type": "INTEGER"},
                                "reason": {"type": "STRING"},
                            },
                            "required": ["symbol", "action", "confidence", "reason"],
                        },
                    },
                },
                "required": ["market_bias", "decisions"],
            },
        },
    }
    if use_groq:
        url = "https://api.groq.com/openai/v1/chat/completions"
        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "You are the market-analysis component of HAN INVEST. "
                    "Only provide advisory signals, never place orders. "
                    "Use only supplied market data. Return strict JSON with "
                    "market_bias and decisions fields."
                )},
                {"role": "user", "content": json.dumps(prompt, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        request_headers = {"Authorization": f"Bearer {api_key}"}
    else:
        url = f"{_API_ROOT}/{model}:generateContent"
        request_body = body
        request_headers = {"x-goog-api-key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = await client.post(
                        url,
                        headers=request_headers,
                        json=request_body,
                    )
                    response.raise_for_status()
                    response_json = response.json()
                    if use_groq:
                        choices = response_json.get("choices") or []
                        content = choices[0].get("message", {}).get("content", "") if choices else ""
                        parsed = json.loads(content)
                        if not isinstance(parsed, dict):
                            raise ValueError("Groq response must be a JSON object")
                    else:
                        parsed = _extract_json(response_json)
                    break
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    if use_groq and status_code == 429:
                        retry_after = exc.response.headers.get("retry-after")
                        cooldown = _set_groq_rate_limit_cooldown(retry_after)
                        logger.warning("Groq rate limited; shared cooldown %.1fs; using Gemini fallback when configured", cooldown)
                        raise
                    if status_code not in _RETRYABLE_STATUS_CODES or attempt >= _MAX_RETRIES:
                        raise
                    retry_after = exc.response.headers.get("retry-after")
                    delay = _retry_delay_seconds(retry_after, attempt)
                    logger.warning(
                        "%s temporary HTTP status=%s; retry %s/%s in %.1fs",
                        provider, status_code, attempt + 1, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt >= _MAX_RETRIES:
                        raise
                    delay = _retry_delay_seconds(None, attempt)
                    logger.warning(
                        "%s temporary network error=%s; retry %s/%s in %.1fs",
                        provider, type(exc).__name__, attempt + 1, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
        _latest = _validated_result(parsed, candidates, model, provider)
        _latest["candidate_fingerprint"] = candidate_fingerprint
        _latest["reused"] = False
        _last_success_fingerprint = candidate_fingerprint
        _last_success_result = dict(_latest)
        logger.info("%s analysis succeeded for %d prefiltered Bybit pairs", provider, len(candidates))
    except httpx.HTTPStatusError as exc:
        if use_groq and gemini_key:
            try:
                _latest = await _gemini_fallback(candidates, body, gemini_key)
                logger.warning("Groq unavailable; Gemini fallback supplied advisory analysis")
                return dict(_latest)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as fallback_exc:
                logger.warning("Gemini fallback failed: %s", type(fallback_exc).__name__)
        # Log only the HTTP status and retry hint. Never log provider bodies or credentials.
        status_code = exc.response.status_code
        retry_after = exc.response.headers.get("retry-after")
        logger.warning(
            "%s market analysis failed: HTTP status=%s retry_after=%s",
            provider, status_code, retry_after or "not-provided",
        )
        _latest = {
            "available": False,
            "mode": "SHADOW",
            "provider": provider,
            "model": model,
            "status": "ERROR",
            "error_http_status": status_code,
            "retry_after": retry_after,
            "market_bias": "WAIT",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": f"{provider} API вернул HTTP {status_code}; PAPER продолжает работу, новые входы ждут свежего AI-подтверждения.",
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        if use_groq and gemini_key:
            try:
                _latest = await _gemini_fallback(candidates, body, gemini_key)
                logger.warning("Groq unavailable; Gemini fallback supplied advisory analysis")
                return dict(_latest)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as fallback_exc:
                logger.warning("Gemini fallback failed: %s", type(fallback_exc).__name__)
        # Never expose provider response bodies or credentials in logs/API output.
        logger.warning("%s market analysis failed: %s", provider, type(exc).__name__)
        _latest = {
            "available": False,
            "mode": "SHADOW",
            "provider": provider,
            "model": model,
            "status": "ERROR",
            "market_bias": "WAIT",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": f"{provider} временно недоступен; PAPER продолжает работу, новые входы ждут свежего AI-подтверждения.",
        }
    return dict(_latest)


def get_ai_advisor_state() -> dict[str, Any]:
    return dict(_latest)




# Open-position exit review is advisory-only until explicitly promoted after testing.
_EXIT_REVIEW_INTERVAL_SECONDS = 110
_last_exit_review_at: datetime | None = None
_latest_exit_review: dict[str, Any] = {
    "available": False,
    "mode": "SHADOW",
    "provider": "Groq",
    "status": "NOT_RUN",
    "recommendations": [],
    "updated_at": None,
    "message": "Проверка открытых позиций Groq ещё не выполнялась.",
}


async def refresh_ai_exit_advisor(
    open_positions: list[dict[str, Any]],
    market_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Review existing PAPER positions with Groq; never execute or close trades."""
    global _last_exit_review_at, _latest_exit_review
    now = datetime.now(timezone.utc)
    if not open_positions:
        _latest_exit_review = {
            "available": True, "mode": "SHADOW", "provider": "Groq",
            "status": "NO_OPEN_POSITIONS", "recommendations": [],
            "updated_at": now.isoformat(),
            "message": "Открытых PAPER-позиций нет; запрос Groq не требуется.",
        }
        _last_exit_review_at = now
        return dict(_latest_exit_review)

    # Avoid extra Groq requests on every fast PAPER worker cycle.
    if (_last_exit_review_at is not None and
            (now - _last_exit_review_at).total_seconds() < _EXIT_REVIEW_INTERVAL_SECONDS and
            _latest_exit_review.get("status") in {"OK", "NO_OPEN_POSITIONS"}):
        return {**_latest_exit_review, "reused": True}

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
    cooldown_remaining = _groq_cooldown_remaining(now)
    if cooldown_remaining > 0:
        _latest_exit_review = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "RATE_LIMITED", "recommendations": [],
            "updated_at": now.isoformat(), "retry_in_seconds": int(cooldown_remaining),
            "message": "Groq временно ограничил запросы; повторная проверка отложена. Stop Loss и защита PAPER работают независимо.",
        }
        _last_exit_review_at = now
        logger.info("Groq PAPER exit review skipped during shared rate-limit cooldown (%.1fs remaining)", cooldown_remaining)
        return dict(_latest_exit_review)
    if not groq_key:
        _latest_exit_review = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "NOT_CONFIGURED", "recommendations": [],
            "updated_at": now.isoformat(),
            "message": "GROQ_API_KEY не настроен; защитные правила PAPER продолжают работать.",
        }
        _last_exit_review_at = now
        return dict(_latest_exit_review)

    assets_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in (market_assets or []) if isinstance(item, dict) and item.get("symbol")
    }
    allowed: dict[str, str] = {}
    positions = []
    for item in open_positions[:20]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        direction = str(item.get("side") or item.get("direction") or "LONG").upper()
        if not symbol or direction not in {"LONG", "SHORT"}:
            continue
        asset = assets_by_symbol.get(symbol, {})
        allowed[symbol] = direction
        positions.append({
            "symbol": symbol,
            "direction": direction,
            "entry_price": item.get("average_entry_price", item.get("entry_price")),
            "current_price": item.get("last_price"),
            "unrealized_pnl_usdt": item.get("unrealized_pnl_usdt"),
            "market_value_usdt": item.get("market_value_usdt"),
            "atr_14_1h": asset.get("atr_14_1h", item.get("atr_14_1h")),
            "change_1h_pct": asset.get("change_1h_pct"),
            "change_24h_pct": asset.get("change_24h_pct"),
            "momentum_6h_pct": asset.get("momentum_6h_pct"),
            "momentum_24h_pct": asset.get("momentum_24h_pct"),
            "relative_strength_vs_btc_24h_pct": asset.get("relative_strength_vs_btc_24h_pct"),
            "volume_ratio": asset.get("volume_ratio"),
            "volume_confirmed": asset.get("volume_confirmed"),
            "ma20": asset.get("ma20"),
            "ma50": asset.get("ma50"),
            "rsi_14": asset.get("rsi_14"),
            "long_score": asset.get("long_score"),
            "short_score": asset.get("short_score"),
        })
    if not positions:
        _latest_exit_review = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "NO_VALID_POSITIONS", "recommendations": [],
            "updated_at": now.isoformat(),
            "message": "Не удалось собрать проверяемые данные открытых позиций.",
        }
        _last_exit_review_at = now
        return dict(_latest_exit_review)

    prompt = {
        "task": (
            "Review ONLY these already-open virtual Bybit Spot PAPER positions. "
            "For each position return HOLD or SELL. Recommend SELL only when supplied "
            "evidence indicates a meaningful adverse trend or deteriorating momentum "
            "for that position's direction. A small ordinary fluctuation is not enough. "
            "Never invent missing indicators or prices. If data is incomplete, mixed, "
            "or not clearly adverse, choose HOLD. This is advisory analysis, not an order."
        ),
        "positions": positions,
    }
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "You are HAN INVEST's PAPER open-position risk reviewer. "
                "Do not place orders. Return strict JSON only with a recommendations array. "
                "Each item must contain symbol, direction, action (HOLD or SELL), "
                "confidence (integer 0-100), and reason."
            )},
            {"role": "user", "content": json.dumps(prompt, separators=(",", ":"), allow_nan=False, default=float)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}"},
                json=request_body,
            )
            response.raise_for_status()
        choices = response.json().get("choices") or []
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("Groq exit review must be a JSON object")

        decisions = {}
        for decision in parsed.get("recommendations", []):
            if not isinstance(decision, dict):
                continue
            symbol = str(decision.get("symbol") or "").upper()
            direction = str(decision.get("direction") or "").upper()
            action = str(decision.get("action") or "HOLD").upper()
            if symbol not in allowed or direction != allowed[symbol] or action not in {"HOLD", "SELL"}:
                continue
            try:
                confidence = max(0, min(100, int(decision.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0
            if action == "SELL" and confidence < 80:
                action = "HOLD"
            decisions[symbol] = {
                "symbol": symbol, "direction": direction, "action": action,
                "confidence": confidence, "reason": str(decision.get("reason") or "")[:240],
            }
        recommendations = [
            decisions.get(symbol, {
                "symbol": symbol, "direction": direction, "action": "HOLD",
                "confidence": 0, "reason": "Нет проверенной рекомендации Groq; позиция не затрагивается.",
            })
            for symbol, direction in sorted(allowed.items())
        ]
        _latest_exit_review = {
            "available": True, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "OK", "recommendations": recommendations,
            "updated_at": now.isoformat(), "auto_close_enabled": False,
            "message": "Только рекомендации. Groq не закрывает позиции; Stop Loss и защита PAPER работают независимо.",
        }
        sells = sum(1 for item in recommendations if item["action"] == "SELL")
        logger.info("Groq PAPER exit review succeeded for %d open positions; SELL recommendations=%d (shadow only)",
                    len(recommendations), sells)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            cooldown = _set_groq_rate_limit_cooldown(exc.response.headers.get("retry-after"))
            logger.warning("Groq PAPER exit review rate limited; shared cooldown %.1fs", cooldown)
        _latest_exit_review = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "RATE_LIMITED" if exc.response.status_code == 429 else "ERROR",
            "recommendations": [], "updated_at": now.isoformat(),
            "retry_in_seconds": int(_groq_cooldown_remaining()),
            "message": "Groq временно ограничил запросы; проверка отложена. Stop Loss и защита PAPER работают независимо." if exc.response.status_code == 429 else f"Groq не смог проверить открытые позиции ({type(exc).__name__}); защитные правила PAPER продолжают работать.",
        }
        logger.warning("Groq PAPER exit review failed: HTTP status=%s", exc.response.status_code)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        _latest_exit_review = {
            "available": False, "mode": "SHADOW", "provider": "Groq",
            "model": model, "status": "ERROR", "recommendations": [],
            "updated_at": now.isoformat(),
            "message": f"Groq не смог проверить открытые позиции ({type(exc).__name__}); защитные правила PAPER продолжают работать.",
        }
        logger.warning("Groq PAPER exit review failed: %s", type(exc).__name__)
    _last_exit_review_at = now
    return dict(_latest_exit_review)


def get_ai_exit_advisor_state() -> dict[str, Any]:
    return dict(_latest_exit_review)


@router.get("/exits/latest")
async def latest_ai_exit_advice():
    return get_ai_exit_advisor_state()


@router.get("/latest")
async def latest_ai_advice():
    return get_ai_advisor_state()
