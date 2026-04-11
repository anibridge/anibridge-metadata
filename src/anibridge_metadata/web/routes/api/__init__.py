"""API route modules."""

from fastapi import APIRouter

from anibridge_metadata.web.routes.api.metadata import router as metadata_router

__all__ = ["router"]

router = APIRouter()

router.include_router(metadata_router, prefix="/metadata", tags=["metadata"])
