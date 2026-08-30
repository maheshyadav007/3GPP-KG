from __future__ import annotations

from hashlib import sha256

import pytest
from botocore.exceptions import ClientError

from threegpp_kg.storage.object_store import LocalObjectStore, S3ObjectStore


@pytest.mark.asyncio
async def test_local_object_store_is_content_addressed_idempotent_and_bounded(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    content = b"immutable artifact"
    digest = sha256(content).hexdigest()
    key = await store.put(digest, "../document.docx", content, "application/octet-stream")
    assert await store.exists(key)
    assert await store.get(key) == content
    assert await store.put(digest, "document.docx", content, "ignored") == key
    with pytest.raises(ValueError, match="escapes"):
        await store.get("../../outside")


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Key: str, Body: bytes, **kwargs) -> None:
        self.objects[Key] = Body

    def get_object(self, *, Key: str, **kwargs):
        content = self.objects[Key]

        class Body:
            def read(self) -> bytes:
                return content

        return {"Body": Body()}

    def head_object(self, *, Key: str, **kwargs) -> None:
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")


@pytest.mark.asyncio
async def test_s3_object_store_put_get_and_exists_without_network() -> None:
    store = object.__new__(S3ObjectStore)
    store.bucket = "test"
    store.client = FakeS3Client()
    digest = "a" * 64
    key = await store.put(digest, "folder/document.docx", b"content", "application/test")
    assert await store.exists(key)
    assert await store.get(key) == b"content"
    assert not await store.exists("missing")
