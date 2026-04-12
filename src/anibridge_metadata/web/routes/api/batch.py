"""Batch metadata lookup via SSE and WebSocket streaming."""

import logging
from collections.abc import AsyncIterator

import orjson
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from anibridge_metadata.services.batch_collector import BatchCollector, BatchResult
from anibridge_metadata.web.dependencies import get_batch_collector, get_settings

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


class BatchRequest(BaseModel):
    """Request body for batch metadata lookups."""

    descriptors: list[str] = Field(..., min_length=1)


async def _sse_stream(
    collector: BatchCollector,
    descriptors: list[str],
) -> AsyncIterator[str]:
    """Yield SSE as results resolve."""
    async for result in collector.stream(descriptors):
        payload = _result_to_dict(result)
        data = orjson.dumps(payload).decode()
        yield f"data: {data}\n\n"
    yield "event: done\ndata: {}\n\n"


def _result_to_dict(result: BatchResult) -> dict:
    """Serialize a BatchResult to a JSON dict."""
    if result.envelope is not None:
        return {
            "descriptor": result.descriptor,
            "status": "ok",
            "data": result.envelope.model_dump(mode="json"),
        }
    return {
        "descriptor": result.descriptor,
        "status": "error",
        "status_code": result.status_code,
        "detail": result.error or "Unknown error",
    }


@router.post("/stream")
async def batch_stream_sse(
    body: BatchRequest,
    collector: BatchCollector = Depends(get_batch_collector),
    settings=Depends(get_settings),
) -> StreamingResponse:
    """Stream batch metadata results as SSE.

    Results are streamed to the client as they resolve. No need to wait
    for all descriptors to finish before seeing results.
    """
    max_size = settings.batch_max_size
    descriptors = body.descriptors[:max_size]
    logger.info("Batch SSE: %d descriptor(s)", len(descriptors))
    return StreamingResponse(
        _sse_stream(collector, descriptors),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.websocket("/ws")
async def batch_websocket(
    websocket: WebSocket,
    collector: BatchCollector = Depends(get_batch_collector),
    settings=Depends(get_settings),
) -> None:
    """Stream batch metadata results over a WebSocket connection.

    Client sends a JSON message: `{"descriptors": ["anilist:21", ...]}`
    Server streams back individual result messages as they resolve, then
    sends `{"event": "done"}`.
    """
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = orjson.loads(raw)
                descriptors = msg.get("descriptors", [])
                if not isinstance(descriptors, list) or not descriptors:
                    await websocket.send_json(
                        {
                            "event": "error",
                            "detail": "descriptors must be a non-empty list",
                        }
                    )
                    continue
                max_size = settings.batch_max_size
                descriptors = descriptors[:max_size]
                logger.info("Batch WS: %d descriptor(s)", len(descriptors))
            except orjson.JSONDecodeError, AttributeError:
                await websocket.send_json({"event": "error", "detail": "Invalid JSON"})
                continue

            async for result in collector.stream(descriptors):
                payload = _result_to_dict(result)
                payload["event"] = "result"
                await websocket.send_bytes(orjson.dumps(payload))

            await websocket.send_bytes(orjson.dumps({"event": "done"}))
    except WebSocketDisconnect:
        logger.debug("Batch WebSocket client disconnected.")
