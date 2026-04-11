from typing import cast

import pytest

from anibridge_metadata.core.config import Settings, TmdbConfig
from anibridge_metadata.core.descriptors import parse_descriptor
from anibridge_metadata.core.enums import EntityType
from anibridge_metadata.services.providers.base import ProviderConfigurationError
from anibridge_metadata.services.providers.tmdb import (
    TmdbAdapter,
    TmdbPayload,
)
from anibridge_metadata.utils.http import HttpClient


class FakeHttpClient:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = responses or [{"id": 123, "title": "Example"}, {}]
        self.calls: list[dict[str, object | None]] = []

    async def get_json(self, url: str, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self.responses:
            raise AssertionError("No fake response configured")
        return dict(self.responses.pop(0))


@pytest.mark.asyncio
async def test_tmdb_adapter_uses_access_token_bearer_auth() -> None:
    http_client = FakeHttpClient(
        responses=[
            {
                "id": 123,
                "title": "Example",
                "external_ids": {"imdb_id": "tt0137523"},
            }
        ]
    )
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token="token-123")),
        http_client=cast(HttpClient, http_client),
    )

    payload = await adapter.fetch_raw(descriptor=parse_descriptor("tmdb_movie:550"))

    assert http_client.calls == [
        {
            "url": "https://api.themoviedb.org/3/movie/550",
            "headers": {"Authorization": "Bearer token-123"},
            "params": {"append_to_response": "external_ids"},
        },
    ]
    assert isinstance(payload, TmdbPayload)
    assert payload.resolved_entity_type == EntityType.MOVIE
    assert payload.external_ids is not None
    assert payload.external_ids.imdb_id == "tt0137523"


def test_tmdb_adapter_requires_access_token() -> None:
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token=None)),
        http_client=cast(HttpClient, FakeHttpClient()),
    )

    with pytest.raises(
        ProviderConfigurationError,
        match="ABM_TMDB__ACCESS_TOKEN",
    ):
        adapter._auth()


@pytest.mark.asyncio
async def test_tmdb_show_normalization_builds_scopes() -> None:
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token="token-123")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TmdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "first_air_date": "2008-01-20",
            "episode_run_time": [48],
            "number_of_episodes": 20,
            "seasons": [
                {
                    "season_number": 1,
                    "name": "Season 1",
                    "episode_count": 7,
                    "air_date": "2008-01-20",
                    "episodes": [
                        {"air_date": "2008-01-20"},
                        {"air_date": "2008-03-09"},
                    ],
                },
                {
                    "season_number": 2,
                    "name": "Season 2",
                    "episode_count": 13,
                    "air_date": "2009-03-08",
                    "episodes": [
                        {"air_date": "2009-03-08"},
                        {"air_date": "2009-05-31"},
                    ],
                },
                {"season_number": 0, "name": "Specials", "episode_count": 2},
            ],
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tmdb_show:1396"),
        payload=payload,
    )

    assert result.scopes is not None
    assert len(result.scopes) == 3
    assert "s0" in result.scopes
    assert result.scopes["s0"].titles.display == "Specials"
    assert result.scopes["s0"].units == 2
    assert result.scopes["s1"].id.descriptor == "tmdb_show:1396:s1"
    assert result.scopes["s1"].id.scope == "s1"
    assert result.scopes["s1"].titles.display == "Season 1"
    assert result.scopes["s1"].units == 7
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.start_date is not None
    assert result.scopes["s1"].release.start_date.isoformat() == "2008-01-20"
    assert result.scopes["s1"].release.end_date is not None
    assert result.scopes["s1"].release.end_date.isoformat() == "2008-03-09"
    assert result.scopes["s1"].runtime is not None
    assert result.scopes["s1"].runtime.minutes == 48
    assert result.scopes["s2"].id.descriptor == "tmdb_show:1396:s2"
    assert result.scopes["s2"].id.scope == "s2"
    assert result.scopes["s2"].titles.display == "Season 2"
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.end_date is None
    assert result.scopes["s0"].release is None
    assert result.units is not None


@pytest.mark.asyncio
async def test_tmdb_show_normalization_builds_twins() -> None:
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token="token-123")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TmdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "first_air_date": "2008-01-20",
            "episode_run_time": [48],
            "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tmdb_show:1396"),
        payload=payload,
    )

    assert [r.target.descriptor for r in result.relationships] == [
        "imdb_show:tt0903747",
        "tvdb_show:81189",
    ]


@pytest.mark.asyncio
async def test_tmdb_show_fetch_uses_append_to_response() -> None:
    basic_response = {
        "id": 1396,
        "name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "status": "Ended",
        "seasons": [
            {"season_number": 0, "name": "Specials", "episode_count": 2},
            {
                "season_number": 1,
                "name": "Season 1",
                "episode_count": 7,
                "air_date": "2008-01-20",
            },
        ],
    }
    enriched_response = {
        **basic_response,
        "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
        "season/0": {"episodes": []},
        "season/1": {
            "episodes": [
                {"air_date": "2008-01-20", "episode_number": 1},
                {"air_date": "2008-03-09", "episode_number": 7},
            ]
        },
    }
    http_client = FakeHttpClient(responses=[basic_response, enriched_response])
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token="token-123")),
        http_client=cast(HttpClient, http_client),
    )

    payload = await adapter.fetch_raw(descriptor=parse_descriptor("tmdb_show:1396"))

    assert len(http_client.calls) == 2
    assert http_client.calls[0]["params"] is None
    assert http_client.calls[1]["params"] == {
        "append_to_response": "external_ids,season/0,season/1"
    }
    assert isinstance(payload, TmdbPayload)
    assert payload.external_ids is not None
    assert payload.external_ids.imdb_id == "tt0903747"
    s1 = next(s for s in payload.seasons if s.season_number == 1)
    assert len(s1.episodes) == 2
    assert s1.episodes[0].air_date == "2008-01-20"
    assert s1.episodes[1].air_date == "2008-03-09"


@pytest.mark.asyncio
async def test_tmdb_show_scope_end_dates_completed_show() -> None:
    adapter = TmdbAdapter(
        settings=Settings(tmdb=TmdbConfig(access_token="token-123")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TmdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "status": "Ended",
            "first_air_date": "2008-01-20",
            "last_air_date": "2013-09-29",
            "episode_run_time": [48],
            "seasons": [
                {
                    "season_number": 0,
                    "name": "Specials",
                    "episode_count": 2,
                    "episodes": [
                        {"air_date": "2009-02-17"},
                        {"air_date": "2010-10-09"},
                    ],
                },
                {
                    "season_number": 1,
                    "name": "Season 1",
                    "episode_count": 7,
                    "air_date": "2008-01-20",
                    "episodes": [
                        {"air_date": "2008-01-20"},
                        {"air_date": "2008-03-09"},
                    ],
                },
                {
                    "season_number": 2,
                    "name": "Season 2",
                    "episode_count": 13,
                    "air_date": "2009-03-08",
                    "episodes": [
                        {"air_date": "2009-03-08"},
                        {"air_date": "2009-05-31"},
                    ],
                },
            ],
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tmdb_show:1396"),
        payload=payload,
    )

    assert result.scopes is not None
    # All seasons get end dates when show is completed.
    assert result.scopes["s0"].release is not None
    assert result.scopes["s0"].release.end_date is not None
    assert result.scopes["s0"].release.end_date.isoformat() == "2010-10-09"
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.end_date is not None
    assert result.scopes["s1"].release.end_date.isoformat() == "2008-03-09"
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.end_date is not None
    assert result.scopes["s2"].release.end_date.isoformat() == "2009-05-31"
