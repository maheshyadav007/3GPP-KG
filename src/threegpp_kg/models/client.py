from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import ModelEndpointConfig
from .base import EmbeddingClient


class ModelEndpointError(RuntimeError):
    pass


class RetryableModelEndpointError(ModelEndpointError):
    pass


def create_model_client(
    config: ModelEndpointConfig,
    *,
    timeout_seconds: float,
    expected_dimensions: int | None = None,
    retries: int = 2,
    client: httpx.AsyncClient | None = None,
) -> OpenAICompatibleClient:
    if config.provider != "openai_compatible":
        raise ModelEndpointError(
            f"model provider {config.provider!r} is not installed in this deployment"
        )
    return OpenAICompatibleClient(
        config,
        timeout_seconds=timeout_seconds,
        expected_dimensions=expected_dimensions,
        retries=retries,
        client=client,
    )


def create_embedding_client(
    config: ModelEndpointConfig,
    *,
    timeout_seconds: float,
    retries: int = 2,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingClient:
    if config.provider == "onnx":
        from .onnx import OnnxEmbeddingClient

        return OnnxEmbeddingClient(config)
    return create_model_client(
        config,
        timeout_seconds=timeout_seconds,
        expected_dimensions=config.dimensions,
        retries=retries,
        client=client,
    )


class OpenAICompatibleClient:
    def __init__(
        self,
        config: ModelEndpointConfig,
        *,
        timeout_seconds: float,
        expected_dimensions: int | None = None,
        retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.expected_dimensions = expected_dimensions or config.dimensions
        self.retries = retries
        self._client = client
        profile_payload = {
            "provider": config.provider,
            "model": config.model,
            "dimensions": self.expected_dimensions,
        }
        digest = hashlib.sha256(json.dumps(profile_payload, sort_keys=True).encode()).hexdigest()
        self.profile_id = f"emb-{digest[:32]}"
        self.model_name = config.model or "unconfigured"
        self.revision = config.revision or "endpoint-managed"
        self.dimensions = self.expected_dimensions or 0

    def available(self) -> bool:
        return bool(self.config.base_url and self.config.model)

    async def warmup(self) -> None:
        await self.embed_queries(["3GPP semantic search warmup"])

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self.embeddings(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embeddings(texts)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key.get_secret_value()}"
        return headers

    def _require_endpoint(self) -> tuple[str, str]:
        if not self.config.base_url or not self.config.model:
            raise ModelEndpointError("model base_url and model must be configured")
        return self.config.base_url.rstrip("/"), self.config.model

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.config.batch_size):
            vectors.extend(
                await self._embedding_batch(texts[start : start + self.config.batch_size])
            )
        return vectors

    async def _embedding_batch(self, texts: list[str]) -> list[list[float]]:
        base_url, model = self._require_endpoint()
        response = await self._post(
            f"{base_url}/v1/embeddings",
            {"model": model, "input": texts},
        )
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ModelEndpointError("embedding response does not match the request batch")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not all(
                isinstance(value, (int, float)) for value in vector
            ):
                raise ModelEndpointError("embedding response contains an invalid vector")
            if self.expected_dimensions and len(vector) != self.expected_dimensions:
                raise ModelEndpointError(
                    f"expected {self.expected_dimensions} dimensions, received {len(vector)}"
                )
            if not all(math.isfinite(float(value)) for value in vector):
                raise ModelEndpointError("embedding response contains non-finite values")
            vectors.append([float(value) for value in vector])
        return vectors

    async def rerank(self, query: str, candidates: list[str]) -> list[tuple[int, float]]:
        if not candidates:
            return []
        if len(candidates) > self.config.batch_size:
            raise ModelEndpointError(
                f"rerank candidate count exceeds batch size {self.config.batch_size}"
            )
        base_url, model = self._require_endpoint()
        response = await self._post(
            f"{base_url}/v1/rerank",
            {"model": model, "query": query, "documents": candidates},
        )
        data = response.get("data", response.get("results"))
        if not isinstance(data, list):
            raise ModelEndpointError("rerank response is missing result data")
        ranked: list[tuple[int, float]] = []
        for item in data:
            if not isinstance(item, dict):
                raise ModelEndpointError("rerank response contains an invalid result")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if (
                not isinstance(index, int)
                or index < 0
                or index >= len(candidates)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ModelEndpointError("rerank response contains an invalid score or index")
            ranked.append((index, float(score)))
        if len({index for index, _ in ranked}) != len(ranked):
            raise ModelEndpointError("rerank response contains duplicate candidate indexes")
        if len(ranked) != len(candidates):
            raise ModelEndpointError("rerank response is partial")
        return sorted(ranked, key=lambda item: (-item[1], item[0]))

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema_name: str,
        schema: dict[str, Any],
        temperature: float = 0,
    ) -> dict[str, Any]:
        base_url, model = self._require_endpoint()
        response = await self._post(
            f"{base_url}/v1/chat/completions",
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
            },
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelEndpointError("chat response is missing message content") from exc
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise ModelEndpointError("chat response content is not JSON text")
        import json

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelEndpointError("chat response contains malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelEndpointError("chat response JSON must be an object")
        return parsed

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(
                (
                    httpx.TransportError,
                    httpx.TimeoutException,
                    RetryableModelEndpointError,
                )
            ),
            stop=stop_after_attempt(self.retries + 1),
            wait=wait_exponential(multiplier=0.25, min=0.25, max=2),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._post_once(url, payload)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ModelEndpointError("model endpoint request failed after retries") from exc
        raise AssertionError("retry loop exited without a result")

    async def _post_once(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(url, headers=self._headers(), json=payload)
            if response.status_code == 429:
                raise RetryableModelEndpointError("model endpoint rate limit exceeded")
            if response.status_code >= 500:
                raise RetryableModelEndpointError(
                    f"model endpoint returned HTTP {response.status_code}"
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ModelEndpointError(
                    f"model endpoint returned HTTP {response.status_code}"
                ) from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise ModelEndpointError("model endpoint returned malformed JSON") from exc
            if not isinstance(data, dict):
                raise ModelEndpointError("model endpoint response must be an object")
            return data
        finally:
            if owns_client:
                await client.aclose()
