"""AniBridge metadata caching server."""

from anibridge_metadata.web.app import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    from anibridge_metadata.core.config import get_settings
    from anibridge_metadata.core.logging import (
        LOG_DATE_FORMAT,
        LOG_FORMAT,
        configure_logging,
    )

    settings = get_settings()
    configure_logging(settings.log_level)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4849,
        timeout_keep_alive=600,
        log_level=settings.log_level.lower(),
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": LOG_FORMAT,
                    "datefmt": LOG_DATE_FORMAT,
                },
                "access": {
                    "format": LOG_FORMAT,
                    "datefmt": LOG_DATE_FORMAT,
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                },
                "access": {
                    "class": "logging.StreamHandler",
                    "formatter": "access",
                    "stream": "ext://sys.stderr",
                },
            },
            "loggers": {
                "uvicorn": {
                    "handlers": ["default"],
                    "level": settings.log_level.upper(),
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["default"],
                    "level": settings.log_level.upper(),
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["access"],
                    "level": settings.log_level.upper(),
                    "propagate": False,
                },
            },
        },
    )
