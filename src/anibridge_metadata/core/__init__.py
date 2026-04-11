"""Core runtime primitives for the metadata service."""

from anibridge_metadata.core.config import Settings, get_settings
from anibridge_metadata.core.db import (
    Base,
    build_engine,
    build_session_factory,
    init_db,
)
from anibridge_metadata.core.descriptors import (
    DescriptorValidationError,
    MetadataDescriptor,
    parse_descriptor,
)

__all__ = [
    "Base",
    "DescriptorValidationError",
    "MetadataDescriptor",
    "Settings",
    "build_engine",
    "build_session_factory",
    "get_settings",
    "init_db",
    "parse_descriptor",
]
