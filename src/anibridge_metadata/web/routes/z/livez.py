"""Liveness probe route."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    """Report that the application process is alive."""
    return {"status": "ok"}
