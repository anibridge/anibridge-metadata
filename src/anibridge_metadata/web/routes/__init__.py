"""API route modules."""

from fastapi import APIRouter

from anibridge_metadata.web.routes.api import router as api_router
from anibridge_metadata.web.routes.z import router as z_router

__all__ = ["router"]

router = APIRouter()

router.include_router(api_router, prefix="/api")
router.include_router(z_router, prefix="")
