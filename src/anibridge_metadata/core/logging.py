"""Centralized logging configuration."""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Libraries that are noisy at INFO level.
_NOISY_LOGGERS = ("httpcore", "httpx", "aiohttp", "aiohttp.access")


def configure_logging(level: str = "INFO") -> None:
    """Set up root logging with a consistent format.

    Call once at process startup, before any loggers emit messages.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        stream=sys.stderr,
        force=True,
    )

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
