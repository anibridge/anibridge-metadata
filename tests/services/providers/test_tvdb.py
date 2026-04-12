from typing import cast

import pytest

from anibridge_metadata.core.config import Settings, TvdbConfig
from anibridge_metadata.core.descriptors import parse_descriptor
from anibridge_metadata.core.enums import EntityType
from anibridge_metadata.services.providers.tvdb import TvdbAdapter, TvdbPayload
from anibridge_metadata.utils.http import HttpClient


class FakeHttpClient:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, object | None]] = []

    async def get_json(self, url: str, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self.responses:
            raise AssertionError("No fake response configured")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_tvdb_movie_normalization_uses_first_release_and_runtime() -> None:
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TvdbPayload.model_validate(
        {
            "_resolved_entity_type": "movie",
            "name": "Fight Club",
            "first_release": {
                "country": "global",
                "date": "1999-10-15",
                "detail": None,
            },
            "runtime": 139,
            "score": 1298345,
            "status": {"name": "Released"},
            "genres": [{"name": "Drama"}, {"name": "Thriller"}],
            "image": "https://example.com/poster.jpg",
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tvdb_movie:247"),
        payload=payload,
    )

    assert result.kind == EntityType.MOVIE
    assert result.release is not None
    assert result.release.start_date is not None
    assert result.release.start_date.isoformat() == "1999-10-15"
    assert result.runtime is not None
    assert result.runtime.minutes == 139
    assert result.ratings is not None
    assert result.ratings.popularity == 1298345
    assert result.ratings.average is None


@pytest.mark.asyncio
async def test_tvdb_show_fetch_uses_series_extended_endpoint() -> None:
    http_client = FakeHttpClient(
        responses=[
            {
                "data": {
                    "name": "BrBa Native",
                    "overview": "Native show overview",
                    "status": {"name": "Ended"},
                    "seasons": [
                        {
                            "id": 30272,
                            "number": 1,
                            "name": "Season 1",
                        },
                        {
                            "id": 30273,
                            "number": 2,
                            "name": "Season 2",
                        },
                    ],
                }
            },
            {
                "data": {
                    "name": "Breaking Bad",
                    "overview": "English show overview",
                    "isPrimary": True,
                    "language": "eng",
                }
            },
        ]
    )
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, http_client),
    )
    adapter._token = "cached-token"

    payload = await adapter.fetch_raw(descriptor=parse_descriptor("tvdb_show:81189"))
    assert isinstance(payload, TvdbPayload)

    assert len(http_client.calls) == 2
    assert (
        http_client.calls[0]["url"]
        == "https://api4.thetvdb.com/v4/series/81189/extended?meta=episodes"
    )
    assert (
        http_client.calls[1]["url"]
        == "https://api4.thetvdb.com/v4/series/81189/translations/eng"
    )
    assert payload.name == "Breaking Bad"
    assert payload.overview == "English show overview"


@pytest.mark.asyncio
async def test_tvdb_movie_fetch_prefers_english_translation() -> None:
    http_client = FakeHttpClient(
        responses=[
            {
                "data": {
                    "name": "Sen to Chihiro no Kamikakushi",
                    "overview": "Native overview",
                    "runtime": 125,
                    "status": {"name": "Released"},
                }
            },
            {
                "data": {
                    "name": "Spirited Away",
                    "overview": "English overview",
                    "isPrimary": True,
                    "language": "eng",
                }
            },
        ]
    )
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, http_client),
    )
    adapter._token = "cached-token"

    payload = await adapter.fetch_raw(descriptor=parse_descriptor("tvdb_movie:123"))
    assert isinstance(payload, TvdbPayload)

    assert payload.name == "Spirited Away"
    assert payload.original_name == "Sen to Chihiro no Kamikakushi"
    assert payload.overview == "English overview"
    assert [call["url"] for call in http_client.calls] == [
        "https://api4.thetvdb.com/v4/movies/123/extended",
        "https://api4.thetvdb.com/v4/movies/123/translations/eng",
    ]


@pytest.mark.asyncio
async def test_tvdb_show_normalization_builds_scopes() -> None:
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TvdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "firstAired": "2008-01-20",
            "averageRuntime": 48,
            "seasons": [
                {"number": 1},
                {"number": 2},
                {"number": 0},
            ],
            "episodes": [
                *[
                    {"seasonNumber": 1, "seasonName": "Season 1", "aired": "2008-01-20"}
                    for _ in range(6)
                ],
                {"seasonNumber": 1, "seasonName": "Season 1", "aired": "2008-03-09"},
                *[
                    {"seasonNumber": 2, "seasonName": "Season 2", "aired": "2009-03-08"}
                    for _ in range(13)
                ],
                *[
                    {"seasonNumber": 0, "seasonName": "Specials", "aired": "2009-02-17"}
                    for _ in range(2)
                ],
            ],
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tvdb_show:81189"),
        payload=payload,
    )

    assert result.scopes is not None
    assert len(result.scopes) == 3
    assert "s0" in result.scopes
    assert result.scopes["s0"].titles.display == "Specials"
    assert result.scopes["s0"].units == 2
    assert result.scopes["s0"].release is not None
    assert result.scopes["s0"].release.start_date is not None
    assert result.scopes["s0"].release.end_date is None  # show not completed
    assert result.scopes["s1"].id.descriptor == "tvdb_show:81189:s1"
    assert result.scopes["s1"].id.scope == "s1"
    assert result.scopes["s1"].titles.display == "Season 1"
    assert result.scopes["s1"].units == 7
    assert result.scopes["s1"].runtime is not None
    assert result.scopes["s1"].runtime.minutes == 48
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.start_date is not None
    assert result.scopes["s1"].release.start_date.isoformat() == "2008-01-20"
    assert result.scopes["s1"].release.end_date is not None  # regular season
    assert result.scopes["s1"].release.end_date.isoformat() == "2008-03-09"
    assert result.scopes["s2"].id.descriptor == "tvdb_show:81189:s2"
    assert result.scopes["s2"].titles.display == "Season 2"
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.start_date is not None
    assert result.scopes["s2"].release.start_date.isoformat() == "2009-03-08"
    assert result.scopes["s2"].release.end_date is None  # last season, no finale
    assert result.units == 20


@pytest.mark.asyncio
async def test_tvdb_show_scope_end_dates_completed_show() -> None:
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TvdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "firstAired": "2008-01-20",
            "averageRuntime": 48,
            "status": {"name": "Ended"},
            "seasons": [
                {"number": 1},
                {"number": 2},
                {"number": 0},
            ],
            "episodes": [
                *[
                    {"seasonNumber": 1, "seasonName": "Season 1", "aired": "2008-01-20"}
                    for _ in range(6)
                ],
                {"seasonNumber": 1, "seasonName": "Season 1", "aired": "2008-03-09"},
                *[
                    {"seasonNumber": 2, "seasonName": "Season 2", "aired": "2009-03-08"}
                    for _ in range(12)
                ],
                {"seasonNumber": 2, "seasonName": "Season 2", "aired": "2009-05-31"},
                *[
                    {"seasonNumber": 0, "seasonName": "Specials", "aired": "2009-02-17"}
                    for _ in range(2)
                ],
            ],
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tvdb_show:81189"),
        payload=payload,
    )

    assert result.scopes is not None
    # Completed show: all seasons get end dates
    assert result.scopes["s0"].release is not None
    assert result.scopes["s0"].release.end_date is not None
    assert result.scopes["s0"].release.end_date.isoformat() == "2009-02-17"
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.end_date is not None
    assert result.scopes["s1"].release.end_date.isoformat() == "2008-03-09"
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.end_date is not None
    assert result.scopes["s2"].release.end_date.isoformat() == "2009-05-31"


@pytest.mark.asyncio
async def test_tvdb_show_scope_end_date_with_season_finale() -> None:
    adapter = TvdbAdapter(
        settings=Settings(tvdb=TvdbConfig(api_key="token")),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = TvdbPayload.model_validate(
        {
            "_resolved_entity_type": "show",
            "name": "Breaking Bad",
            "firstAired": "2008-01-20",
            "averageRuntime": 48,
            "seasons": [
                {"number": 1},
                {"number": 2},
                {"number": 0},
            ],
            "episodes": [
                *[
                    {"seasonNumber": 1, "seasonName": "Season 1", "aired": "2008-01-20"}
                    for _ in range(7)
                ],
                *[
                    {"seasonNumber": 2, "seasonName": "Season 2", "aired": "2009-03-08"}
                    for _ in range(12)
                ],
                {
                    "seasonNumber": 2,
                    "seasonName": "Season 2",
                    "aired": "2009-05-31",
                    "finaleType": "season",
                },
                *[
                    {"seasonNumber": 0, "seasonName": "Specials", "aired": "2009-02-17"}
                    for _ in range(2)
                ],
            ],
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("tvdb_show:81189"),
        payload=payload,
    )

    assert result.scopes is not None
    # Show ongoing but last season has a finale episode
    assert result.scopes["s0"].release is not None
    assert result.scopes["s0"].release.end_date is None  # specials: show not completed
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.end_date is not None  # finale present
    assert result.scopes["s2"].release.end_date.isoformat() == "2009-05-31"
