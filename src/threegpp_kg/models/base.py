from __future__ import annotations

from typing import Protocol


class EmbeddingClient(Protocol):
    profile_id: str
    model_name: str
    revision: str
    dimensions: int

    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def warmup(self) -> None: ...

    async def close(self) -> None: ...

    def available(self) -> bool: ...
