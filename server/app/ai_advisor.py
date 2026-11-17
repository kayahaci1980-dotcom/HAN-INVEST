import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ai/advisor", tags=["ai-advisor"])

_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT_SECONDS = 25
_MAX_ASSETS = 12
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
    selected = sorted(
        (item for item in assets if isinstance(item, dict)),
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
                      model: str) -> dict[str, Any]:
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
            "reason": "Gemini не вернул проверенное решение для этой пары.",
        })
        for symbol in sorted(allowed)
    ]
    bias = str(raw.get("market_bias") or "WAIT").upper()
    if bias not in {"LONG", "SHORT", "MIXED", "WAIT"}:
        bias = "WAIT"
    return {
        "available": True,
        "mode": "SHADOW",
        "provider": "Google Gemini",
        "model": model,
        "status": "OK",
        "market_bias": bias,
        "recommendations": recommendations,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": "Теневые рекомендации: сделки не открываются и не закрываются.",
    }


async def refresh_ai_advisor(market: dict[str, Any]) -> dict[str, Any]:
    """Ask Gemini for shadow LONG/SHORT/WAIT advice; never execute trades."""
    global _latest
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    if not api_key:
        _latest = {
            **_latest,
            "available": False,
            "mode": "SHADOW",
            "model": model,
            "status": "NOT_CONFIGURED",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": "Gemini API key is not configured on the server.",
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
    url = f"{_API_ROOT}/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key},
                json=body,
            )
            response.raise_for_status()
            parsed = _extract_json(response.json())
        _latest = _validated_result(parsed, candidates, model)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        # Never expose provider response bodies or credentials in logs/API output.
        logger.warning("Gemini market analysis failed: %s", type(exc).__name__)
        _latest = {
            "available": False,
            "mode": "SHADOW",
            "provider": "Google Gemini",
            "model": model,
            "status": "ERROR",
            "market_bias": "WAIT",
            "recommendations": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": "Gemini временно недоступен; рекомендации не обновлены.",
        }
    return dict(_latest)


def get_ai_advisor_state() -> dict[str, Any]:
    return dict(_latest)


@router.get("/latest")
async def latest_ai_advice():
    return get_ai_advisor_state()
