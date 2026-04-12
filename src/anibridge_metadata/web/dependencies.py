"""FastAPI dependency providers."""

from fastapi import Request

from anibridge_metadata.core.config import Settings
from anibridge_metadata.services.batch_collector import BatchCollector
from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.services.providers.registry import ProviderRegistry
from anibridge_metadata.services.resolver import Resolver


def get_settings(request: Request) -> Settings:
    """Return application settings from app state."""
    return request.app.state.settings


def get_provider_registry(request: Request) -> ProviderRegistry:
    """Return the provider registry from app state."""
    return request.app.state.provider_registry


def get_cache(request: Request) -> CacheLayer:
    """Return the Redis cache layer from app state."""
    return request.app.state.cache


def get_resolver(request: Request) -> Resolver:
    """Return the resolver from app state."""
    return request.app.state.resolver


def get_batch_collector(request: Request) -> BatchCollector:
    """Return the batch collector from app state."""
    return request.app.state.batch_collector
