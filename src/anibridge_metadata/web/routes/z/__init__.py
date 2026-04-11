"""Liveness and readiness probe routes."""

from fastapi import APIRouter

from anibridge_metadata.web.routes.z.livez import router as livez_router
from anibridge_metadata.web.routes.z.readyz import router as readyz_router

__all__ = ["router"]

router = APIRouter()

router.include_router(livez_router)
router.include_router(readyz_router)
