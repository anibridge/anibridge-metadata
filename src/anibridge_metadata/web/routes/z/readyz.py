"""Readiness probe route."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.web.dependencies import get_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/readyz", include_in_schema=False)
async def readyz(
    cache: Annotated[CacheLayer, Depends(get_cache)],
) -> dict[str, str]:
    """Report application and Redis readiness."""
    try:
        await cache.ping()
    except Exception:
        logger.exception("Readiness check failed: Redis unreachable")
        raise
    return {"status": "ok"}
