"""
provider.py - base class for providers
"""

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)


##### EXCEPTIONS #####
class ProviderError(Exception):
    """Base class for all provider-related errors."""


class RequestError(ProviderError):
    """Raised when a request fails permanently or retries are exhausted."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        attempts: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.attempts = attempts
        self.__cause__ = cause


class ParseError(ProviderError):
    """Raised when fetched content cannot be parsed into the expected shape."""


##### UTIL FUNCTIONS #####
def generate_random_headers(referer: str = "", origin: str = "") -> dict[str, str]:
    """Generate a dictionary of common HTTP headers."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    ]

    accept_languages = [
        "en-US,en;q=0.9",
        "en-GB,en;q=0.9",
        "en-US,en;q=0.5",
        "en;q=0.8",
    ]

    headers: dict[str, str] = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": random.choice(user_agents),
        "Accept-Language": random.choice(accept_languages),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin

    return headers


##### DATA CLASSES #####
@dataclass(slots=True)
class ComicInfo:
    title: str
    status: str
    tags: list[str] = field(default_factory=list)
    identifier: str = ""
    thumbnail_url: str = ""
    url: str = ""
    authors: list[str] = field(default_factory=list)
    release_year: str = ""
    item_type: str = ""

@dataclass(slots=True)
class ComicChapter:
    title: str
    url: str
    identifier: str


##### PROVIDER ABSTRACT BASE CLASS #####
class Provider(ABC):
    name: str = ""
    max_retries: int = 3
    referer: str = ""
    timeout: float = 5.0

    def __init__(
        self,
        proxy: str = "",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        follow_redirects: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._owns_client = client is None

        if client is not None:
            self.client = client
        else:
            self.client = httpx.AsyncClient(
                proxy=proxy or None,
                timeout=self.timeout if timeout is None else timeout,
                follow_redirects=follow_redirects,
                headers=headers,
            )

        self.client.headers.update(generate_random_headers(self.referer))

    async def close(self) -> None:
        """Close the underlying client if this provider created it."""
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    async def safe_request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ) -> httpx.Response:
        """
        Perform an HTTP request with retries.

        Raises:
            RequestError: if the request fails permanently or all retries are exhausted.
        """
        last_error: Exception | None = None
        method = method.upper()

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                )

                if response.status_code == 200:
                    return response

                if 500 <= response.status_code < 600:
                    last_error = RequestError(
                        f"Server error {response.status_code}",
                        url=url,
                        status_code=response.status_code,
                        attempts=attempt,
                    )
                else:
                    raise RequestError(
                        f"Request failed with status {response.status_code}",
                        url=url,
                        status_code=response.status_code,
                        attempts=attempt,
                    )

            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as e:
                last_error = e
            except RequestError as e:
                last_error = e
                if e.status_code is not None and 400 <= e.status_code < 500:
                    raise

            if attempt < self.max_retries:
                delay = min(2 ** (attempt - 1), 8)
                logger.debug(
                    "Request attempt %s/%s failed for %s; retrying in %ss",
                    attempt,
                    self.max_retries,
                    url,
                    delay,
                )
                await asyncio.sleep(delay)

        raise RequestError(
            f"Failed to fetch {url} after {self.max_retries} attempts",
            url=url,
            attempts=self.max_retries,
            cause=last_error,
        ) from last_error

    async def safe_get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Convenience wrapper for GET requests."""
        return await self.safe_request(
            url, params=params, headers=headers, method="GET"
        )

    @abstractmethod
    async def search(self, query: str) -> list[ComicInfo]:
        """Search for the given query and return a list of results."""

    @abstractmethod
    async def get_chapter_list(self, item: ComicInfo) -> list[ComicChapter]:
        """Return the chapter list for the given item."""

    @abstractmethod
    async def get_chapters_images(self, item: ComicChapter) -> list[str]:
        """Return the image URLs for the given item."""
