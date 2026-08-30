from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx

from threegpp_kg.config import load_settings
from threegpp_kg.ingestion.download import (
    DownloadError,
    SafeDownloader,
    UnsafeArchiveError,
    validate_zip,
)


def zip_bytes(name: str, content: bytes = b"ok") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, content)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_download_hashes_and_honors_not_modified() -> None:
    config = load_settings().http.model_copy(update={"requests_per_second": 1000})
    async with httpx.AsyncClient() as client:
        downloader = SafeDownloader(config, {"www.3gpp.org"}, client)
        with respx.mock:
            route = respx.get("https://www.3gpp.org/file.zip").mock(
                return_value=httpx.Response(
                    200, content=zip_bytes("doc.docx"), headers={"etag": "v1"}
                )
            )
            result = await downloader.download("https://www.3gpp.org/file.zip")
            assert result is not None and len(result.sha256) == 64 and result.etag == "v1"
            assert route.called
        with respx.mock:
            respx.get("https://www.3gpp.org/file.zip").mock(return_value=httpx.Response(304))
            assert await downloader.download("https://www.3gpp.org/file.zip", etag="v1") is None


@pytest.mark.asyncio
async def test_download_rejects_untrusted_host() -> None:
    downloader = SafeDownloader(load_settings().http, {"www.3gpp.org"})
    with pytest.raises(DownloadError, match="not allowed"):
        await downloader.download("https://evil.example/file.zip")


@pytest.mark.asyncio
async def test_download_retries_transport_and_detects_replacement_at_same_url() -> None:
    config = load_settings().http.model_copy(update={"requests_per_second": 1000, "retries": 1})
    async with httpx.AsyncClient() as client:
        downloader = SafeDownloader(config, {"www.3gpp.org"}, client)
        with respx.mock:
            route = respx.get("https://www.3gpp.org/file.zip").mock(
                side_effect=[
                    httpx.ConnectError("temporary"),
                    httpx.Response(200, content=zip_bytes("doc.docx", b"v1")),
                    httpx.Response(200, content=zip_bytes("doc.docx", b"v2")),
                ]
            )
            first = await downloader.download("https://www.3gpp.org/file.zip")
            second = await downloader.download("https://www.3gpp.org/file.zip")
            assert first and second and first.sha256 != second.sha256
            assert route.call_count == 3


@pytest.mark.asyncio
async def test_download_retries_rate_limit_and_server_errors() -> None:
    config = load_settings().http.model_copy(update={"requests_per_second": 1000, "retries": 2})
    async with httpx.AsyncClient() as client:
        downloader = SafeDownloader(config, {"www.3gpp.org"}, client)
        with respx.mock:
            route = respx.get("https://www.3gpp.org/file.zip").mock(
                side_effect=[
                    httpx.Response(429),
                    httpx.Response(503),
                    httpx.Response(200, content=zip_bytes("doc.docx")),
                ]
            )
            assert await downloader.download("https://www.3gpp.org/file.zip")
            assert route.call_count == 3


@pytest.mark.asyncio
async def test_download_enforces_stream_size_limit() -> None:
    config = load_settings().http.model_copy(
        update={"requests_per_second": 1000, "max_download_bytes": 1024}
    )
    async with httpx.AsyncClient() as client:
        downloader = SafeDownloader(config, {"www.3gpp.org"}, client)
        with respx.mock:
            respx.get("https://www.3gpp.org/large.bin").mock(
                return_value=httpx.Response(200, content=b"x" * 1025)
            )
            with pytest.raises(DownloadError, match="exceeds"):
                await downloader.download("https://www.3gpp.org/large.bin")


def test_zip_path_traversal_is_rejected() -> None:
    with pytest.raises(UnsafeArchiveError, match="unsafe archive member"):
        validate_zip(zip_bytes("../escape.docx"), load_settings().http)
