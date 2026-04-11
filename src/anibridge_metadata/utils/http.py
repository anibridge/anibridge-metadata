"""Shared async HTTP client helpers."""

from typing import Any

import aiohttp
from anibridge.utils.limiter import Limiter


class HttpClientError(RuntimeError):
    """Raised when an upstream HTTP request fails."""

    def __init__(self, status_code: int, body: str) -> None:
        """Capture basic HTTP failure context."""
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body}")


class HttpClient:
    """Thin wrapper around aiohttp for upstream provider calls."""

    def __init__(
        self, *, timeout_seconds: float, user_agent: str, limiter: Limiter | None = None
    ) -> None:
        """Initialize client settings without opening a session yet."""
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._user_agent = user_agent
        self._limiter = limiter
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Create the underlying aiohttp session if needed."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform a GET request and decode the JSON body."""
        response = await self._request("GET", url, headers=headers, params=params)
        try:
            return await response.json(content_type=None)
        except aiohttp.ContentTypeError as exc:
            text = await response.text()
            raise HttpClientError(response.status, text) from exc

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> str:
        """Perform a GET request and return the response body as text."""
        response = await self._request("GET", url, headers=headers, params=params)
        try:
            return await response.text()
        except aiohttp.ContentTypeError as exc:
            text = await response.text()
            raise HttpClientError(response.status, text) from exc

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a POST request and decode the JSON body."""
        response = await self._request(
            "POST", url, headers=headers, json_body=json_body
        )
        try:
            return await response.json(content_type=None)
        except aiohttp.ContentTypeError as exc:
            text = await response.text()
            raise HttpClientError(response.status, text) from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> aiohttp.ClientResponse:
        """Execute a request and return the raw response."""
        session = await self._ensure_session()
        if self._limiter is not None:
            await self._limiter.acquire(asynchronous=True)
        response = await session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
        )
        if response.status >= 400:
            text = await response.text()
            raise HttpClientError(response.status, text)
        return response

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return an initialized aiohttp session."""
        if self._session is None:
            await self.start()
        if self._session is None:
            raise RuntimeError("HTTP session could not be initialized.")
        return self._session
