import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.bybit import scan_bybit

logger = logging.getLogger(__name__)
router = APIRouter()
_SCAN_INTERVAL_SECONDS = 60
_worker_task: asyncio.Task | None = None
_scan_lock = asyncio.Lock()
_latest_snapshot: dict[str, Any] | None = None
_last_error: str | None = None


async def _refresh_snapshot() -> None:
    global _latest_snapshot, _last_error
    async with _scan_lock:
        try:
            payload = await scan_bybit(
                limit=100,
                min_volume=100_000,
                capital_usdt=100.0,
                risk_pct=1.0,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
                raise ValueError("Bybit analysis returned an invalid payload")
            _latest_snapshot = {
                **payload,
                "background_analysis": True,
                "background_updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "Bybit Spot",
            }
            _last_error = None
            logger.info("Background Bybit analysis refreshed: %s assets",
                        len(_latest_snapshot["assets"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _last_error = type(exc).__name__
            logger.exception("Background Bybit analysis refresh failed")


async def _worker() -> None:
    while True:
        await _refresh_snapshot()
        await asyncio.sleep(_SCAN_INTERVAL_SECONDS)


def start_analysis_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def stop_analysis_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


@router.get("/api/v1/crypto/bybit/analysis-latest")
async def latest_bybit_analysis(
    capital_usdt: float = Query(100.0, gt=0),
    risk_pct: float = Query(1.0, ge=0.1, le=5.0),
):
    if capital_usdt != 100.0 or risk_pct != 1.0:
        try:
            return await scan_bybit(limit=1000, min_volume=100_000,
                                    capital_usdt=capital_usdt, risk_pct=risk_pct)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Данные Bybit Spot временно недоступны.") from exc
    if _latest_snapshot is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Фоновый анализ Bybit ещё выполняется.",
                "last_error": _last_error,
            },
        )
    return _latest_snapshot
