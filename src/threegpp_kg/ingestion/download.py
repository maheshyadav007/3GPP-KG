from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ..config import HttpConfig


class DownloadError(RuntimeError):
    pass


class UnsafeArchiveError(DownloadError):
    pass


class RetryableDownloadError(DownloadError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadedArtifact:
    url: str
    content: bytes
    sha256: str
    content_type: str
    etag: str | None
    last_modified: str | None


class SafeDownloader:
    def __init__(
        self,
        config: HttpConfig,
        allowed_hosts: set[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.allowed_hosts = allowed_hosts
        self._client = client
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._last_request = 0.0
        self._rate_lock = asyncio.Lock()

    async def download(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> DownloadedArtifact | None:
        host = urlparse(url).hostname
        if host not in self.allowed_hosts or not url.startswith("https://"):
            raise DownloadError(f"source URL is not allowed: {url}")
        headers = {"User-Agent": self.config.user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        async with self._semaphore:
            await self._rate_limit()
            retrying = AsyncRetrying(
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.TimeoutException, RetryableDownloadError)
                ),
                stop=stop_after_attempt(self.config.retries + 1),
                wait=wait_random_exponential(multiplier=0.25, min=0.25, max=4),
                reraise=True,
            )
            async for attempt in retrying:
                with attempt:
                    result = await self._request(url, headers)
                    break
            else:
                raise AssertionError("retry loop exited without a result")
        if result and zipfile.is_zipfile(io.BytesIO(result.content)):
            await asyncio.to_thread(validate_zip, result.content, self.config)
        return result

    async def _request(self, url: str, headers: dict[str, str]) -> DownloadedArtifact | None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            async with client.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as response:
                if response.status_code == 304:
                    return None
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise RetryableDownloadError(
                        f"source returned retryable HTTP {response.status_code} for {url}"
                    )
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.config.max_download_bytes:
                        raise DownloadError(
                            f"artifact exceeds {self.config.max_download_bytes} bytes"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                return DownloadedArtifact(
                    url=url,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    content_type=response.headers.get("content-type", "application/octet-stream"),
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        finally:
            if owns_client:
                await client.aclose()

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = asyncio.get_running_loop().time()
            interval = 1 / self.config.requests_per_second
            wait_for = interval - (now - self._last_request)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request = asyncio.get_running_loop().time()


def validate_zip(content: bytes, config: HttpConfig) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if len(members) > config.max_archive_members:
            raise UnsafeArchiveError("archive contains too many members")
        expanded = 0
        for member in members:
            normalized = member.filename.replace("\\", "/")
            parts = [part for part in normalized.split("/") if part]
            if normalized.startswith("/") or ".." in parts:
                raise UnsafeArchiveError(f"unsafe archive member: {member.filename}")
            expanded += member.file_size
            if expanded > config.max_archive_uncompressed_bytes:
                raise UnsafeArchiveError("archive expands beyond the configured limit")
