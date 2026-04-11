"""Metadata lookup routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from anibridge_metadata.core.descriptors import DescriptorValidationError
from anibridge_metadata.models.metadata import MetadataEnvelope
from anibridge_metadata.services.cache import CacheService
from anibridge_metadata.services.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    UpstreamNotFoundError,
    UpstreamResponseError,
)
from anibridge_metadata.web.dependencies import get_cache_service

__all__ = ["router"]

router = APIRouter()


@router.get("/{descriptor:path}", response_model=MetadataEnvelope)
async def get_metadata(
    descriptor: str,
    force_refresh: bool = Query(default=False),
    service: CacheService = Depends(get_cache_service),
) -> MetadataEnvelope:
    """Lookup metadata for a descriptor string."""
    try:
        return await service.get_metadata(
            descriptor=descriptor,
            force_refresh=force_refresh,
        )
    except DescriptorValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ProviderConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except UpstreamNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UpstreamResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
