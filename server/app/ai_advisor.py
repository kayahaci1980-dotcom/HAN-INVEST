import asyncio
import hashlib
import json
import logging
import os
import re
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
_GROQ_MAX_RATE_LIMIT_COOLDOWN_SECONDS = 86400
_groq_cooldown_until: datetime | None = None

_GROQ_RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def _parse_provider_delay(value: str | None) -> float | None:
    """Parse Retry-After seconds/date and Groq reset durations such as '1m2.5s'."""
    if not value:
        return None
    raw = str(value).strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        pass

    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)", raw.lower())
    if not parts or "".join(number + unit for number, unit in parts).replace(" ", "") != re.sub(r"\s+", "", raw.lower()):
        return None
    multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(number) * multipliers[unit] for number, unit in parts)


def _groq_rate_limit_diagnostics(headers: Any) -> dict[str, str]:
    """Return only documented rate-limit headers; never log credentials or response bodies."""
    diagnostics: dict[str, str] = {}
    for name in ("retry-after",) + _GROQ_RATE_LIMIT_HEADERS:
        value = headers.get(name)
        if value is not None:
            diagnostics[name] = str(value)[:80]
    return diagnostics


def _groq_cooldown_delay(retry_after: str | None, headers: Any = None) -> float:
    delays: list[float] = []
    retry_delay = _parse_provider_delay(retry_after)
    if retry_delay is not None:
        delays.append(retry_delay)
    if headers is not None:
        for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            reset_delay = _parse_provider_delay(headers.get(name))
            if reset_delay is not None:
                delays.append(reset_delay)
    # Respect the provider's longest reset hint. Use the safety fallback only
    # when Groq supplied no parseable timing information.
    delay = max(delays) if delays else float(_GROQ_RATE_LIMIT_COOLDOWN_SECONDS)
    return min(float(_GROQ_MAX_RATE_LIMIT_COOLDOWN_SECONDS), max(1.0, delay))


def _groq_cooldown_remaining(now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    if _groq_cooldown_until is None:
        return 0.0
    return max(0.0, (_groq_cooldown_until - current).total_seconds())


def _set_groq_rate_limit_cooldown(
    retry_after: str | None = None, headers: Any = None
) -> float:
    global _groq_cooldown_until
    delay = _groq_cooldown_delay(retry_after, headers)
    _groq_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return delay
_MAX_ASSETS = 500
_last_success_fingerprint: str | None = None
_last_success_result: dict[str, Any] | None = None
_latest: dict[str, Any] = {
    "available": False,
    "mode": "SHADOW",
    "provider": "Bybit Spot",
    "model": "native-market-analysis",
    "status": "DISABLED",
    "market_bias": "WAIT",
    "recommendations": [],
    "updated_at": None,
    "message": "Groq и Gemini отключены. Анализ выполняется только по данным Bybit Spot.",
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
            if max(float(item.get("long_score") or 0), float(item.get("short_score") or 0)) < 70:
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
    action_counts = {
        action: sum(1 for item in recommendations if item["action"] == action)
        for action in ("LONG", "SHORT", "WAIT")
    }
    logger.info(
        "%s advisory counts: submitted_pairs=%d; validated_model_decisions=%d; LONG=%d; SHORT=%d; WAIT=%d; missing_default_WAIT=%d",
        provider, len(candidates), len(decisions),
        action_counts["LONG"], action_counts["SHORT"], action_counts["WAIT"],
        max(0, len(allowed) - len(decisions)),
    )
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
    """External AI providers are disabled; Bybit data is the sole source."""
    global _latest
    _latest = {
        "available": False,
        "mode": "DISABLED",
        "provider": "Bybit Spot",
        "model": "native-market-analysis",
        "status": "DISABLED",
        "market_bias": "WAIT",
        "recommendations": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": "Groq и Gemini отключены. Используются только рыночные данные и оценки Bybit Spot.",
    }
    return dict(_latest)


def get_ai_advisor_state() -> dict[str, Any]:
    return dict(_latest)




# Open-position exit review is advisory-only until explicitly promoted after testing.
_EXIT_REVIEW_INTERVAL_SECONDS = 940
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


async def _gemini_exit_review_fallback(
    positions: list[dict[str, Any]],
    allowed: dict[str, str],
    api_key: str,
    now: datetime,
) -> dict[str, Any]:
    """Review the same PAPER positions with Gemini; recommendations remain shadow-only."""
    model = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
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
    body = {
        "system_instruction": {"parts": [{
            "text": (
                "You are HAN INVEST's PAPER open-position risk reviewer. "
                "Do not place orders. Return strict JSON only with a recommendations array. "
                "Each item must contain symbol, direction, action (HOLD or SELL), "
                "confidence (integer 0-100), and reason."
            )
        }]},
        "contents": [{"role": "user", "parts": [{
            "text": json.dumps(prompt, separators=(",", ":"), allow_nan=False, default=float)
        }]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "recommendations": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "symbol": {"type": "STRING"},
                                "direction": {"type": "STRING", "enum": ["LONG", "SHORT"]},
                                "action": {"type": "STRING", "enum": ["HOLD", "SELL"]},
                                "confidence": {"type": "INTEGER"},
                                "reason": {"type": "STRING"},
                            },
                            "required": ["symbol", "direction", "action", "confidence", "reason"],
                        },
                    }
                },
                "required": ["recommendations"],
            },
        },
    }
    url = f"{_API_ROOT}/{model}:generateContent"
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers={"x-goog-api-key": api_key}, json=body)
        response.raise_for_status()
        parsed = _extract_json(response.json())

    decisions: dict[str, dict[str, Any]] = {}
    for item in parsed.get("recommendations", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        direction = str(item.get("direction") or "").upper()
        action = str(item.get("action") or "HOLD").upper()
        if symbol not in allowed or direction != allowed[symbol] or action not in {"HOLD", "SELL"}:
            continue
        try:
            confidence = max(0, min(100, int(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        if action == "SELL" and confidence < 80:
            action = "HOLD"
        decisions[symbol] = {
            "symbol": symbol, "direction": direction, "action": action,
            "confidence": confidence, "reason": str(item.get("reason") or "")[:240],
        }

    recommendations = [
        decisions.get(symbol, {
            "symbol": symbol, "direction": direction, "action": "HOLD",
            "confidence": 0,
            "reason": "Gemini не вернул проверенную рекомендацию; позиция не затрагивается.",
        })
        for symbol, direction in sorted(allowed.items())
    ]
    sells = sum(1 for item in recommendations if item["action"] == "SELL")
    logger.info(
        "Gemini PAPER exit review fallback succeeded for %d open positions; SELL recommendations=%d (shadow only)",
        len(recommendations), sells,
    )
    return {
        "available": True, "mode": "SHADOW", "provider": "Google Gemini (fallback)",
        "model": model, "status": "OK", "recommendations": recommendations,
        "updated_at": now.isoformat(), "auto_close_enabled": False,
        "message": "Резервные рекомендации Gemini. Сделки не закрываются автоматически; Stop Loss и защита PAPER работают независимо.",
    }


async def refresh_ai_exit_advisor(
    open_positions: list[dict[str, Any]],
    market_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """External AI exit reviews are disabled; built-in PAPER protections remain active."""
    global _latest_exit_review
    _latest_exit_review = {
        "available": False,
        "mode": "DISABLED",
        "provider": "Bybit Spot",
        "status": "DISABLED",
        "recommendations": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "auto_close_enabled": False,
        "message": "Проверки Groq/Gemini отключены. Защитные правила PAPER работают самостоятельно.",
    }
    return dict(_latest_exit_review)


def get_ai_exit_advisor_state() -> dict[str, Any]:
    return dict(_latest_exit_review)


@router.get("/exits/latest")
async def latest_ai_exit_advice():
    return get_ai_exit_advisor_state()


@router.get("/latest")
async def latest_ai_advice():
    return get_ai_advisor_state()
