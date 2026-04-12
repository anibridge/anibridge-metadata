"""Batch metadata lookup routes."""

import asyncio

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from anibridge_metadata.core.descriptors import DescriptorValidationError
from anibridge_metadata.models.metadata import MetadataEnvelope
from anibridge_metadata.services.cache import CacheService
from anibridge_metadata.services.providers.base import ProviderError
from anibridge_metadata.web.dependencies import get_cache_service

__all__ = ["router"]

router = APIRouter()

_MAX_BATCH_SIZE = 50


class BatchRequest(BaseModel):
    """Request body for batch metadata lookups."""

    descriptors: list[str] = Field(..., min_length=1, max_length=_MAX_BATCH_SIZE)


class BatchItemError(BaseModel):
    """Error detail for a single descriptor in a batch response."""

    status_code: int
    detail: str


class BatchResponse(BaseModel):
    """Response envelope for batch metadata lookups."""

    results: dict[str, MetadataEnvelope] = Field(default_factory=dict)
    errors: dict[str, BatchItemError] = Field(default_factory=dict)


def _error_for_exception(exc: Exception) -> BatchItemError:
    """Map a provider/validation exception to a batch error entry."""
    from anibridge_metadata.services.providers.base import (
        ProviderConfigurationError,
        UpstreamNotFoundError,
        UpstreamResponseError,
    )

    if isinstance(exc, DescriptorValidationError):
        return BatchItemError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    if isinstance(exc, ProviderConfigurationError):
        return BatchItemError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(exc, UpstreamNotFoundError):
        return BatchItemError(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, UpstreamResponseError):
        return BatchItemError(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, ProviderError):
        return BatchItemError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
    return BatchItemError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error",
    )


async def _fetch_one(
    descriptor: str, service: CacheService
) -> MetadataEnvelope | Exception:
    """Fetch a single descriptor, returning the envelope or the exception."""
    try:
        return await service.get_metadata(descriptor=descriptor)
    except Exception as exc:
        return exc


@router.post("", response_model=BatchResponse)
async def batch_get_metadata(
    body: BatchRequest,
    service: CacheService = Depends(get_cache_service),
) -> BatchResponse:
    """Lookup metadata for multiple descriptors in one request."""
    unique_descriptors = list(dict.fromkeys(body.descriptors))

    tasks = [_fetch_one(d, service) for d in unique_descriptors]
    outcomes = await asyncio.gather(*tasks)

    results: dict[str, MetadataEnvelope] = {}
    errors: dict[str, BatchItemError] = {}

    for descriptor, outcome in zip(unique_descriptors, outcomes, strict=False):
        if isinstance(outcome, Exception):
            errors[descriptor] = _error_for_exception(outcome)
        else:
            results[descriptor] = outcome

    return BatchResponse(results=results, errors=errors)
