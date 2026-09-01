from __future__ import annotations

import json

import httpx
import pytest
import respx

from threegpp_kg.config import ModelEndpointConfig
from threegpp_kg.models import ModelEndpointError, OpenAICompatibleClient, create_model_client


def model_config(dimensions: int = 3) -> ModelEndpointConfig:
    return ModelEndpointConfig(
        provider="openai_compatible",
        base_url="https://models.internal",
        model="embedding-model",
        dimensions=dimensions,
        api_key="secret",
        batch_size=2,
    )


@pytest.mark.asyncio
async def test_embeddings_validate_order_dimensions_and_auth() -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            route = respx.post("https://models.internal/v1/embeddings").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 1, "embedding": [0, 1, 0]},
                            {"index": 0, "embedding": [1, 0, 0]},
                        ]
                    },
                )
            )
            vectors = await client.embeddings(["first", "second"])
            assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
            assert route.calls[0].request.headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_embeddings_reject_invalid_dimensions() -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            respx.post("https://models.internal/v1/embeddings").mock(
                return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})
            )
            with pytest.raises(ModelEndpointError, match="expected 3 dimensions"):
                await client.embeddings(["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(200, text="bad-json"), httpx.Response(500)])
async def test_endpoint_failures_are_sanitized(response: httpx.Response) -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            respx.post("https://models.internal/v1/embeddings").mock(return_value=response)
            with pytest.raises(ModelEndpointError):
                await client.embeddings(["text"])


@pytest.mark.asyncio
async def test_unconfigured_endpoint_fails_before_network() -> None:
    client = OpenAICompatibleClient(
        ModelEndpointConfig(provider="openai_compatible"), timeout_seconds=1
    )
    with pytest.raises(ModelEndpointError, match="must be configured"):
        await client.embeddings(["text"])


def test_unavailable_model_provider_fails_explicitly() -> None:
    with pytest.raises(ModelEndpointError, match="not installed"):
        create_model_client(
            ModelEndpointConfig(
                provider="onnx", model="local-model", revision="a" * 40, dimensions=3
            ),
            timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_embeddings_batch_requests_and_retry_rate_limit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429)
        inputs = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index + 1), 0, 0]}
                    for index, _ in enumerate(inputs)
                ]
            },
        )

    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, retries=1, client=http)
        with respx.mock:
            route = respx.post("https://models.internal/v1/embeddings").mock(side_effect=handler)
            vectors = await client.embeddings(["one", "two", "three"])
    assert len(vectors) == 3
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried() -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, retries=3, client=http)
        with respx.mock:
            route = respx.post("https://models.internal/v1/embeddings").mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(ModelEndpointError, match="HTTP 401"):
                await client.embeddings(["text"])
            assert route.call_count == 1


@pytest.mark.asyncio
async def test_rerank_stable_ties_empty_and_batch_limit() -> None:
    config = model_config()
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(config, timeout_seconds=2, client=http)
        assert await client.rerank("query", []) == []
        with respx.mock:
            respx.post("https://models.internal/v1/rerank").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 1, "relevance_score": 0.7},
                            {"index": 0, "relevance_score": 0.7},
                        ]
                    },
                )
            )
            assert await client.rerank("query", ["a", "b"]) == [(0, 0.7), (1, 0.7)]
        with pytest.raises(ModelEndpointError, match="batch size"):
            await client.rerank("query", ["a", "b", "c"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,message",
    [
        ({"data": []}, "does not match"),
        ({"data": [{"index": 0, "embedding": "bad"}]}, "invalid vector"),
        ({"data": [{"index": 0, "embedding": [float("nan"), 0, 0]}]}, "non-finite"),
    ],
)
async def test_embeddings_reject_malformed_payloads(payload, message: str) -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            respx.post("https://models.internal/v1/embeddings").mock(
                return_value=httpx.Response(
                    200,
                    content=json.dumps(payload).encode(),
                    headers={"content-type": "application/json"},
                )
            )
            with pytest.raises(ModelEndpointError, match=message):
                await client.embeddings(["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,message",
    [
        ({}, "missing result"),
        ({"data": ["bad", {"index": 1, "score": 0.2}]}, "invalid result"),
        ({"data": [{"index": 3, "score": 0.2}, {"index": 0, "score": 0.1}]}, "score or index"),
        ({"data": [{"index": 0, "score": 0.2}, {"index": 0, "score": 0.1}]}, "duplicate"),
        ({"data": [{"index": 0, "score": 0.2}]}, "partial"),
    ],
)
async def test_rerank_rejects_malformed_and_partial_payloads(payload, message: str) -> None:
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            respx.post("https://models.internal/v1/rerank").mock(
                return_value=httpx.Response(200, json=payload)
            )
            with pytest.raises(ModelEndpointError, match=message):
                await client.rerank("query", ["a", "b"])


@pytest.mark.asyncio
async def test_chat_json_accepts_object_or_json_text_and_rejects_bad_content() -> None:
    responses = [
        {"choices": [{"message": {"content": {"answer": 1}}}]},
        {"choices": [{"message": {"content": '{"answer": 2}'}}]},
        {"choices": []},
        {"choices": [{"message": {"content": "not-json"}}]},
        {"choices": [{"message": {"content": "[]"}}]},
    ]
    async with httpx.AsyncClient() as http:
        client = OpenAICompatibleClient(model_config(), timeout_seconds=2, client=http)
        with respx.mock:
            route = respx.post("https://models.internal/v1/chat/completions").mock(
                side_effect=[httpx.Response(200, json=item) for item in responses]
            )
            assert await client.chat_json([], schema_name="test", schema={}) == {"answer": 1}
            assert await client.chat_json([], schema_name="test", schema={}) == {"answer": 2}
            with pytest.raises(ModelEndpointError, match="missing message"):
                await client.chat_json([], schema_name="test", schema={})
            with pytest.raises(ModelEndpointError, match="malformed JSON"):
                await client.chat_json([], schema_name="test", schema={})
            with pytest.raises(ModelEndpointError, match="must be an object"):
                await client.chat_json([], schema_name="test", schema={})
            assert route.call_count == 5
