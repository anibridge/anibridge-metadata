"""Readiness probe route."""

import logging

from fastapi import APIRouter, Depends

from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.web.dependencies import get_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/readyz", include_in_schema=False)
async def readyz(
    cache: CacheLayer = Depends(get_cache),
) -> dict[str, str]:
    """Report application and Redis readiness."""
    try:
        await cache.ping()
    except Exception:
        logger.error("Readiness check failed: Redis unreachable", exc_info=True)
        raise
    return {"status": "ok"}
