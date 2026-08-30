from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from ..config import ObjectStoreConfig


class ObjectStore(Protocol):
    async def put(self, sha256: str, filename: str, content: bytes, content_type: str) -> str: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError("object key escapes the configured root")
        return candidate

    async def put(self, sha256: str, filename: str, content: bytes, content_type: str) -> str:
        del content_type
        safe_name = Path(filename).name
        key = f"sha256/{sha256[:2]}/{sha256}/{safe_name}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            await asyncio.to_thread(path.write_bytes, content)
        return key

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ObjectStore:
    def __init__(self, config: ObjectStoreConfig) -> None:
        self.bucket = config.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=(config.access_key.get_secret_value() if config.access_key else None),
            aws_secret_access_key=(
                config.secret_key.get_secret_value() if config.secret_key else None
            ),
        )

    async def put(self, sha256: str, filename: str, content: bytes, content_type: str) -> str:
        key = f"sha256/{sha256[:2]}/{sha256}/{Path(filename).name}"
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            Metadata={"sha256": sha256},
        )
        return key

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False
