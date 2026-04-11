import pytest

from anibridge_metadata.core.config import (
    AniDbConfig,
    AnilistConfig,
    ImdbConfig,
    MalConfig,
    Settings,
    TmdbConfig,
    TvdbConfig,
)
from anibridge_metadata.core.enums import DescriptorProvider
from anibridge_metadata.services.providers.base import ProviderConfigurationError
from anibridge_metadata.services.providers.registry import ProviderRegistry


def test_provider_registry_applies_rate_limiters_to_selected_providers() -> None:
    settings = Settings(
        anidb=AniDbConfig(enabled=True),
        anilist=AnilistConfig(enabled=True),
        imdb=ImdbConfig(enabled=True),
        mal=MalConfig(enabled=True),
        tmdb=TmdbConfig(enabled=True),
        tvdb=TvdbConfig(enabled=True),
    )
    registry = ProviderRegistry(settings=settings)

    assert registry.get(DescriptorProvider.ANIDB).http_client._limiter is not None
    assert registry.get(DescriptorProvider.ANILIST).http_client._limiter is not None
    assert registry.get(DescriptorProvider.MAL).http_client._limiter is not None
    assert registry.get(DescriptorProvider.IMDB_MOVIE).http_client._limiter is None
    assert registry.get(DescriptorProvider.IMDB_SHOW).http_client._limiter is None
    assert registry.get(DescriptorProvider.TMDB_MOVIE).http_client._limiter is None
    assert registry.get(DescriptorProvider.TMDB_SHOW).http_client._limiter is None
    assert registry.get(DescriptorProvider.TVDB_MOVIE).http_client._limiter is None
    assert registry.get(DescriptorProvider.TVDB_SHOW).http_client._limiter is None


def test_provider_registry_rejects_disabled_providers() -> None:
    registry = ProviderRegistry(
        settings=Settings(
            anidb=AniDbConfig(enabled=False),
            tmdb=TmdbConfig(enabled=False),
        )
    )

    with pytest.raises(
        ProviderConfigurationError,
        match="ABM_ANIDB__ENABLED",
    ):
        registry.get(DescriptorProvider.ANIDB)

    with pytest.raises(
        ProviderConfigurationError,
        match="ABM_TMDB__ENABLED",
    ):
        registry.get(DescriptorProvider.TMDB_SHOW)


@pytest.mark.asyncio
async def test_provider_registry_runs_provider_lifecycle_hooks() -> None:
    registry = ProviderRegistry(settings=Settings())

    started = False
    closed = False

    class FakeProvider:
        async def start(self) -> None:
            nonlocal started
            started = True

        async def close(self) -> None:
            nonlocal closed
            closed = True

    registry._providers = {DescriptorProvider.ANIDB: FakeProvider()}
    registry._http_clients = {}

    await registry.start()
    await registry.close()

    assert started is True
    assert closed is True
