"""Shared enum types used across the service."""

from enum import StrEnum


class DescriptorProvider(StrEnum):
    """Supported descriptor provider namespaces."""

    ANIDB = "anidb"
    ANILIST = "anilist"
    IMDB_MOVIE = "imdb_movie"
    IMDB_SHOW = "imdb_show"
    MAL = "mal"
    TMDB_MOVIE = "tmdb_movie"
    TMDB_SHOW = "tmdb_show"
    TVDB_MOVIE = "tvdb_movie"
    TVDB_SHOW = "tvdb_show"


class EntityType(StrEnum):
    """Unified metadata entity types exposed by the service."""

    MOVIE = "movie"
    SHOW = "show"


class TitleStatus(StrEnum):
    """Normalized lifecycle states for a title."""

    UPCOMING = "upcoming"
    ONGOING = "ongoing"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    HIATUS = "hiatus"
    UNKNOWN = "unknown"


class ImageType(StrEnum):
    """Normalized image categories stored for a title."""

    BANNER = "banner"
    COVER = "cover"
    POSTER = "poster"
    THUMBNAIL = "thumbnail"
    UNKNOWN = "unknown"
