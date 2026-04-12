"""Metadata lookup routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from anibridge_metadata.core.descriptors import DescriptorValidationError
from anibridge_metadata.models.metadata import MetadataEnvelope
from anibridge_metadata.services.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    UpstreamNotFoundError,
    UpstreamResponseError,
)
from anibridge_metadata.services.resolver import Resolver
from anibridge_metadata.web.dependencies import get_resolver

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{descriptor:path}", response_model=MetadataEnvelope)
async def get_metadata(
    descriptor: str,
    force_refresh: bool = Query(default=False),
    resolver: Resolver = Depends(get_resolver),
) -> MetadataEnvelope:
    """Lookup metadata for a descriptor string."""
    try:
        envelope = await resolver.resolve(
            descriptor=descriptor,
            force_refresh=force_refresh,
        )
        logger.info(
            "%s -> %s [%s]",
            descriptor,
            envelope.cache.source,
            envelope.metadata.titles.display if envelope.metadata else "n/a",
        )
        return envelope
    except DescriptorValidationError as exc:
        logger.warning("Invalid descriptor %r: %s", descriptor, exc)
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
