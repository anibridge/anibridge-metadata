from __future__ import annotations

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


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        redis_url="redis://localhost:6379/15",
        anidb=AniDbConfig(enabled=False),
        anilist=AnilistConfig(enabled=False),
        imdb=ImdbConfig(enabled=False),
        mal=MalConfig(enabled=False),
        tmdb=TmdbConfig(enabled=False),
        tvdb=TvdbConfig(enabled=False),
    )
