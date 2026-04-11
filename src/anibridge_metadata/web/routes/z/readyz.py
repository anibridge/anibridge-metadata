"""Readiness probe route."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from anibridge_metadata.core.db import ping_db
from anibridge_metadata.web.dependencies import get_db_session

router = APIRouter()


@router.get("/readyz", include_in_schema=False)
async def readyz(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Report application and database readiness."""
    await ping_db(session)
    return {"status": "ok"}
