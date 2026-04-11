from typing import cast

import pytest

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import parse_descriptor
from anibridge_metadata.core.enums import EntityType, TitleStatus
from anibridge_metadata.services.providers.base import UpstreamNotFoundError
from anibridge_metadata.services.providers.imdb import (
    ImdbAdapter,
    ImdbPayload,
    ImdbSeasonPayload,
)
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


def test_imdb_scoped_descriptor_parses_to_parent() -> None:
    descriptor = parse_descriptor("imdb_show:tt0903747:s1")

    assert descriptor.scope == "s1"
    assert descriptor.parent is not None
    assert descriptor.parent.key == "imdb_show:tt0903747"


@pytest.mark.asyncio
async def test_imdb_fetch_raw_uses_qlever_query_endpoint() -> None:
    http_client = FakeHttpClient(
        responses=[
            {
                "head": {"vars": ["canonicalId", "primaryTitle"]},
                "results": {
                    "bindings": [
                        {
                            "canonicalId": {"type": "literal", "value": "tt0137523"},
                            "primaryTitle": {"type": "literal", "value": "Fight Club"},
                            "typeValue": {"type": "literal", "value": "movie"},
                        }
                    ]
                },
            }
        ]
    )
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, http_client),
    )

    payload = await adapter.fetch_raw(
        descriptor=parse_descriptor("imdb_movie:tt0137523")
    )

    assert isinstance(payload, ImdbPayload)
    assert payload.canonical_id == "tt0137523"
    assert payload.primary_title == "Fight Club"
    assert http_client.calls == [
        {
            "url": "https://qlever.dev/api/imdb",
            "headers": None,
            "params": {"query": adapter._build_query("tt0137523")},
        }
    ]


@pytest.mark.asyncio
async def test_imdb_show_fetch_raw_loads_season_aggregates() -> None:
    http_client = FakeHttpClient(
        responses=[
            {
                "head": {"vars": ["canonicalId", "primaryTitle", "typeValue"]},
                "results": {
                    "bindings": [
                        {
                            "canonicalId": {"type": "literal", "value": "tt0903747"},
                            "primaryTitle": {
                                "type": "literal",
                                "value": "Breaking Bad",
                            },
                            "typeValue": {"type": "literal", "value": "tvSeries"},
                        }
                    ]
                },
            },
            {
                "head": {
                    "vars": [
                        "season",
                        "episodeCount",
                        "seasonStartYear",
                        "seasonEndYear",
                    ]
                },
                "results": {
                    "bindings": [
                        {
                            "season": {"type": "literal", "value": "1"},
                            "episodeCount": {"type": "literal", "value": "7"},
                            "seasonStartYear": {"type": "literal", "value": "2008"},
                            "seasonEndYear": {"type": "literal", "value": "2008"},
                        },
                        {
                            "season": {"type": "literal", "value": "2"},
                            "episodeCount": {"type": "literal", "value": "13"},
                            "seasonStartYear": {"type": "literal", "value": "2009"},
                            "seasonEndYear": {"type": "literal", "value": "2009"},
                        },
                    ]
                },
            },
        ]
    )
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, http_client),
    )

    payload = await adapter.fetch_raw(
        descriptor=parse_descriptor("imdb_show:tt0903747")
    )

    assert isinstance(payload, ImdbPayload)
    assert [season.season_number for season in payload.seasons] == [1, 2]
    assert payload.episode_count == 20
    assert http_client.calls == [
        {
            "url": "https://qlever.dev/api/imdb",
            "headers": None,
            "params": {"query": adapter._build_query("tt0903747")},
        },
        {
            "url": "https://qlever.dev/api/imdb",
            "headers": None,
            "params": {"query": adapter._build_season_query("tt0903747")},
        },
    ]


@pytest.mark.asyncio
async def test_imdb_fetch_raw_raises_not_found_for_missing_title() -> None:
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(
            HttpClient,
            FakeHttpClient(
                responses=[
                    {
                        "head": {"vars": ["canonicalId", "primaryTitle"]},
                        "results": {"bindings": []},
                    }
                ]
            ),
        ),
    )

    with pytest.raises(UpstreamNotFoundError, match="IMDB did not find"):
        await adapter.fetch_raw(descriptor=parse_descriptor("imdb_movie:tt9999999999"))


@pytest.mark.asyncio
async def test_imdb_normalize_maps_live_qlever_fields() -> None:
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = ImdbPayload(
        canonical_id="tt0903747",
        label="Breaking Bad",
        primary_title="Breaking Bad",
        original_title="Breaking Bad",
        type_value="tvSeries",
        is_adult=False,
        start_year=2008,
        end_year=2013,
        runtime_minutes=45,
        average_rating=9.5,
        num_votes=2418405,
        genres=["Crime", "Drama", "Crime"],
        aliases=["Breaking Bad", "Total szivas", "Breaking Bad Italy"],
        seasons=[
            ImdbSeasonPayload(
                season_number=0,
                episode_count=2,
                start_year=2009,
                end_year=2010,
            ),
            ImdbSeasonPayload(
                season_number=1,
                episode_count=7,
                start_year=2008,
                end_year=2008,
            ),
            ImdbSeasonPayload(
                season_number=2,
                episode_count=13,
                start_year=2009,
                end_year=2009,
            ),
        ],
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("imdb_show:tt0903747"),
        payload=payload,
    )

    assert result.kind == EntityType.SHOW
    assert result.titles.display == "Breaking Bad"
    assert result.titles.main == "Breaking Bad"
    assert result.titles.original == "Breaking Bad"
    assert result.titles.aliases == ["Total szivas", "Breaking Bad Italy"]
    assert result.release is not None
    assert result.release.status == TitleStatus.FINISHED
    assert result.release.start_date is None
    assert result.release.end_date is None
    assert result.runtime is not None
    assert result.runtime.minutes == 45
    assert result.runtime.basis == "derived"
    assert result.units == 22
    assert result.classification.genres == ["Crime", "Drama"]
    assert result.classification.is_adult is False
    assert result.ratings is not None
    assert result.ratings.average == 9.5
    assert result.ratings.popularity == 2418405.0
    assert result.scopes is not None
    assert set(result.scopes) == {"s0", "s1", "s2"}
    assert result.scopes["s0"].titles.display == "Specials"
    assert result.scopes["s0"].units == 2
    assert result.scopes["s0"].release is not None
    assert result.scopes["s0"].release.status == TitleStatus.FINISHED
    assert result.scopes["s0"].release.start_date is None
    assert result.scopes["s1"].titles.display == "Breaking Bad Season 1"
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.status == TitleStatus.FINISHED
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.status == TitleStatus.FINISHED
    assert result.source == "https://www.imdb.com/title/tt0903747"


@pytest.mark.asyncio
async def test_imdb_scope_status_marks_last_season_ongoing_when_show_is_ongoing() -> (
    None
):
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = ImdbPayload(
        canonical_id="tt1234567",
        label="Example Show",
        primary_title="Example Show",
        type_value="tvSeries",
        start_year=2024,
        seasons=[
            ImdbSeasonPayload(
                season_number=1,
                episode_count=10,
                start_year=2024,
                end_year=2024,
            ),
            ImdbSeasonPayload(
                season_number=2,
                episode_count=8,
                start_year=2025,
                end_year=2025,
            ),
        ],
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("imdb_show:tt1234567"),
        payload=payload,
    )

    assert result.release is not None
    assert result.release.status == TitleStatus.ONGOING
    assert result.release.start_date is None
    assert result.release.end_date is None
    assert result.scopes is not None
    assert result.scopes["s1"].release is not None
    assert result.scopes["s1"].release.status == TitleStatus.FINISHED
    assert result.scopes["s2"].release is not None
    assert result.scopes["s2"].release.status == TitleStatus.ONGOING


@pytest.mark.asyncio
async def test_imdb_normalize_rejects_descriptor_type_mismatch() -> None:
    adapter = ImdbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )

    with pytest.raises(UpstreamNotFoundError, match="descriptor namespace"):
        await adapter.normalize(
            descriptor=parse_descriptor("imdb_movie:tt0903747"),
            payload=ImdbPayload(
                canonical_id="tt0903747",
                primary_title="Breaking Bad",
                type_value="tvSeries",
            ),
        )
