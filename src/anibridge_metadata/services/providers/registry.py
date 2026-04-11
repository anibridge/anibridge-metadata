"""Registry for upstream provider adapters."""

from anibridge.utils.limiter import Limiter

from anibridge_metadata.core.config import RateLimiterConfig, Settings
from anibridge_metadata.core.enums import DescriptorProvider
from anibridge_metadata.services.providers.anidb import AniDbAdapter
from anibridge_metadata.services.providers.anilist import AnilistAdapter
from anibridge_metadata.services.providers.base import ProviderAdapter
from anibridge_metadata.services.providers.imdb import ImdbAdapter
from anibridge_metadata.services.providers.mal import MalAdapter
from anibridge_metadata.services.providers.tmdb import TmdbAdapter
from anibridge_metadata.services.providers.tvdb import TvdbAdapter
from anibridge_metadata.utils.http import HttpClient


class ProviderRegistry:
    """Resolve descriptor provider values to concrete adapter instances."""

    def __init__(self, *, settings: Settings) -> None:
        """Create a registry populated with all supported providers."""
        client_keys = {
            "anidb": settings.anidb.rate_limiter,
            "anilist": settings.anilist.rate_limiter,
            "imdb": settings.imdb.rate_limiter,
            "mal": settings.mal.rate_limiter,
            "tmdb": settings.tmdb.rate_limiter,
            "tvdb": settings.tvdb.rate_limiter,
        }
        clients = {
            key: self._build_http_client(settings=settings, rate_limiter=rate_limiter)
            for key, rate_limiter in client_keys.items()
        }
        provider_specs: dict[DescriptorProvider, tuple[type[ProviderAdapter], str]] = {
            DescriptorProvider.ANIDB: (AniDbAdapter, "anidb"),
            DescriptorProvider.ANILIST: (AnilistAdapter, "anilist"),
            DescriptorProvider.IMDB_MOVIE: (ImdbAdapter, "imdb"),
            DescriptorProvider.IMDB_SHOW: (ImdbAdapter, "imdb"),
            DescriptorProvider.MAL: (MalAdapter, "mal"),
            DescriptorProvider.TMDB_MOVIE: (TmdbAdapter, "tmdb"),
            DescriptorProvider.TMDB_SHOW: (TmdbAdapter, "tmdb"),
            DescriptorProvider.TVDB_MOVIE: (TvdbAdapter, "tvdb"),
            DescriptorProvider.TVDB_SHOW: (TvdbAdapter, "tvdb"),
        }
        self._http_clients = {
            provider: clients[client_key]
            for provider, (_, client_key) in provider_specs.items()
        }
        self._providers = {
            provider: adapter_type(settings=settings, http_client=clients[client_key])
            for provider, (adapter_type, client_key) in provider_specs.items()
        }

    def get(self, provider: DescriptorProvider) -> ProviderAdapter:
        """Return the adapter for a specific provider."""
        return self._providers[provider]

    async def start(self) -> None:
        """Start all provider HTTP clients."""
        for http_client in self._unique_http_clients():
            await http_client.start()

    async def close(self) -> None:
        """Close all provider HTTP clients."""
        for http_client in self._unique_http_clients():
            await http_client.close()

    def _unique_http_clients(self) -> set[HttpClient]:
        """Return unique HTTP client instances from the registry."""
        return set(self._http_clients.values())

    def _build_http_client(
        self,
        *,
        settings: Settings,
        rate_limiter: RateLimiterConfig | None = None,
    ) -> HttpClient:
        """Create an HTTP client with an optional provider limiter."""
        limiter: Limiter | None = None
        if rate_limiter is not None:
            limiter = Limiter(rate=rate_limiter.rate, capacity=rate_limiter.capacity)

        return HttpClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
            limiter=limiter,
        )
