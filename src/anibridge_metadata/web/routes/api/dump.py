"""Database dump API route."""

from collections.abc import AsyncIterator

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from anibridge_metadata.web.dependencies import get_db_session

__all__ = ["router"]

router = APIRouter()


async def _stream_records(session: AsyncSession) -> AsyncIterator[str]:
    """Stream metadata records as a JSON object keyed by descriptor."""
    result = await session.stream(
        text(
            "SELECT descriptor, normalized_payload FROM metadata_records ORDER BY "
            "descriptor"
        )
    )
    yield "{"
    first = True
    async for descriptor, raw_payload in result:
        if not first:
            yield ","
        first = False
        yield orjson.dumps(descriptor).decode() + ":" + raw_payload
    yield "}"


@router.get("")
async def dump(
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Dump all cached metadata records as a JSON object keyed by descriptor."""
    return StreamingResponse(
        _stream_records(session),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="anibridge-metadata.json"'
        },
    )
