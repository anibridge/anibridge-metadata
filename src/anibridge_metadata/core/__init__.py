"""Core runtime primitives for the metadata service."""

from anibridge_metadata.core.config import Settings, get_settings
from anibridge_metadata.core.descriptors import (
    DescriptorValidationError,
    MetadataDescriptor,
    parse_descriptor,
)

__all__ = [
    "DescriptorValidationError",
    "MetadataDescriptor",
    "Settings",
    "get_settings",
    "parse_descriptor",
]
