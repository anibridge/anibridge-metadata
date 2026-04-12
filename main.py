"""AniBridge metadata caching server."""

from anibridge_metadata.web.app import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4849,
        timeout_keep_alive=600,
    )
