from pathlib import Path
from typing import cast

import pytest

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import (
    DescriptorValidationError,
    parse_descriptor,
)
from anibridge_metadata.core.enums import EntityType, TitleStatus
from anibridge_metadata.services.providers.anidb import AniDbAdapter, AniDbPayload
from anibridge_metadata.utils.http import HttpClient


class FakeHttpClient:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_anidb_scoped_descriptor_parses_to_parent() -> None:
    descriptor = parse_descriptor("anidb:1:r")

    assert descriptor.scope == "R"
    assert descriptor.parent is not None
    assert descriptor.parent.key == "anidb:1"


def test_anidb_rejects_unknown_scope_code() -> None:
    with pytest.raises(DescriptorValidationError, match="AniDB scopes"):
        parse_descriptor("anidb:1:Z")


@pytest.mark.asyncio
async def test_anidb_fetch_raw_reads_local_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "anime-aggregations"
    anime_path = repo_path / "anime"
    anime_path.mkdir(parents=True)
    (anime_path / "1.json").write_text(
        """
        {
          "anime_id": 1,
          "titles": [
            {"language": "ENGLISH", "title": "Crest of the Stars", "type": "OFFICIAL"}
          ]
        }
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(AniDbAdapter, "REPOSITORY_PATH", repo_path)

    adapter = AniDbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    adapter._repository_ready = True

    payload = await adapter.fetch_raw(descriptor=parse_descriptor("anidb:1"))

    assert isinstance(payload, AniDbPayload)
    assert payload.anime_id == 1
    assert payload.titles[0].title == "Crest of the Stars"


@pytest.mark.asyncio
async def test_anidb_normalize_maps_titles_runtime_and_relationships() -> None:
    adapter = AniDbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = AniDbPayload.model_validate(
        {
            "anime_id": 1,
            "description": "Space opera",
            "start_date": "1999-01-03",
            "end_date": "1999-03-28",
            "episodes": {
                "REGULAR": [
                    {"length": 25, "number": 1},
                    {"length": 25, "number": 2},
                ]
            },
            "ratings": {"PERMANENT": {"rating": 8.23, "total": 5056}},
            "resources": {
                "IMDB": ["tt0286390"],
                "MAL": ["290"],
                "TMDB": ["tv/26209"],
            },
            "tags": {
                "1": {"name": "military", "weight": 300, "spoiler": False},
                "2": {"name": "maintenance", "weight": 0, "spoiler": False},
            },
            "titles": [
                {
                    "language": "JAPANESE_TRANSLITERATED",
                    "title": "Seikai no Monshou",
                    "type": "MAIN",
                },
                {
                    "language": "ENGLISH",
                    "title": "Crest of the Stars",
                    "type": "OFFICIAL",
                },
                {
                    "language": "JAPANESE",
                    "title": "星界の紋章",
                    "type": "OFFICIAL",
                },
                {
                    "language": "ENGLISH",
                    "title": "CotS",
                    "type": "SHORT",
                },
            ],
            "type": "SERIES",
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("anidb:1"),
        payload=payload,
    )

    assert result.kind == EntityType.SHOW
    assert result.titles.display == "Crest of the Stars"
    assert result.titles.main == "Seikai no Monshou"
    assert result.titles.original == "星界の紋章"
    assert result.titles.aliases == ["CotS"]
    assert result.runtime is not None
    assert result.runtime.minutes == 25
    assert result.units == 2
    assert result.release is not None
    assert result.release.status == TitleStatus.FINISHED
    assert result.classification.genres == ["military"]
    assert result.scopes is not None
    assert result.scopes["R"].id.descriptor == "anidb:1:R"
    assert result.scopes["R"].units == 2
    assert result.scopes["R"].runtime is not None
    assert result.scopes["R"].runtime.minutes == 25
    assert [
        relationship.target.descriptor for relationship in result.relationships
    ] == [
        "imdb_show:tt0286390",
        "mal:290",
        "tmdb_show:26209",
    ]


@pytest.mark.asyncio
async def test_anidb_normalize_builds_episode_type_scopes() -> None:
    adapter = AniDbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )
    payload = AniDbPayload.model_validate(
        {
            "anime_id": 1,
            "titles": [
                {
                    "language": "ENGLISH",
                    "title": "Crest of the Stars",
                    "type": "OFFICIAL",
                }
            ],
            "episodes": {
                "REGULAR": [
                    {"air_date": "1999-01-03", "length": 25, "number": 1},
                    {"air_date": "1999-03-28", "length": 40, "number": 13},
                ],
                "CREDITS": [
                    {"air_date": "1999-01-03", "length": 2, "number": 1},
                    {"air_date": "1999-03-28", "length": 0, "number": 2},
                ],
            },
            "type": "SERIES",
        }
    )

    result = await adapter.normalize(
        descriptor=parse_descriptor("anidb:1"),
        payload=payload,
    )

    assert result.scopes is not None
    assert set(result.scopes) == {"R", "C"}
    assert result.scopes["C"].id.scope == "C"
    assert result.scopes["C"].units == 2
    assert result.scopes["C"].runtime is not None
    assert result.scopes["C"].runtime.minutes == 2
    assert result.scopes["C"].release is not None
    assert result.scopes["C"].release.start_date is not None
    assert result.scopes["C"].release.start_date.isoformat() == "1999-01-03"


@pytest.mark.asyncio
async def test_anidb_start_schedules_daily_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AniDbAdapter(
        settings=Settings(),
        http_client=cast(HttpClient, FakeHttpClient()),
    )

    sync_calls = 0

    async def fake_sync() -> None:
        nonlocal sync_calls
        sync_calls += 1
        adapter._repository_ready = True

    monkeypatch.setattr(adapter, "_sync_repository", fake_sync)
    monkeypatch.setattr(adapter, "_seconds_until_next_sync", lambda: 3600.0)

    await adapter.start()

    assert sync_calls == 1
    assert adapter._sync_task is not None

    await adapter.close()

    assert adapter._sync_task is None
