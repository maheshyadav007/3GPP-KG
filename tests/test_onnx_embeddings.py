from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from threegpp_kg.config import ModelEndpointConfig
from threegpp_kg.models import ModelEndpointError
from threegpp_kg.models.onnx import (
    OnnxEmbeddingClient,
    embedding_profile_id,
    embedding_profile_path,
)


def onnx_config(tmp_path: Path, *, dimensions: int = 3) -> ModelEndpointConfig:
    return ModelEndpointConfig(
        provider="onnx",
        model="test/model",
        revision="a" * 40,
        dimensions=dimensions,
        cache_dir=tmp_path,
        batch_size=2,
        max_concurrency=1,
        query_cache_size=2,
    )


def write_manifest(config: ModelEndpointConfig) -> None:
    directory = embedding_profile_path(config)
    (directory / "onnx").mkdir(parents=True, exist_ok=True)
    (directory / "onnx" / "model.onnx").write_bytes(b"test")
    (directory / "threegpp-embedding-manifest.json").write_text(
        json.dumps(
            {
                "profile_id": embedding_profile_id(config),
                "onnx_file": "onnx/model.onnx",
                "onnx_sha256": hashlib.sha256(b"test").hexdigest(),
                "runtime_version": "test",
            }
        ),
        encoding="utf-8",
    )


class FakeSentenceTransformer:
    def __init__(self, dimensions: int, calls: list[dict[str, Any]]) -> None:
        self.dimensions = dimensions
        self.calls = calls
        self.max_seq_length = 0
        self._modules: dict[str, Any] = {}

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.calls.append({"texts": texts, "max_seq_length": self.max_seq_length, **kwargs})
        vector = [1.0, *([0.0] * (self.dimensions - 1))]
        return [vector.copy() for _ in texts]


@pytest.mark.asyncio
async def test_onnx_client_batches_normalizes_caches_and_uses_local_model(tmp_path: Path) -> None:
    config = onnx_config(tmp_path)
    write_manifest(config)
    calls: list[dict[str, Any]] = []

    def factory(*args: Any, **kwargs: Any) -> FakeSentenceTransformer:
        calls.append({"factory_args": args, "factory_kwargs": kwargs})
        return FakeSentenceTransformer(3, calls)

    client = OnnxEmbeddingClient(config, model_factory=factory)
    assert client.available() is True
    first = await client.embed_queries(["R2-240001"])
    second = await client.embed_queries(["R2-240001"])
    documents = await client.embed_documents(["one", "two", "three"])

    assert first == second == [[1.0, 0.0, 0.0]]
    assert len(documents) == 3
    encode_calls = [call for call in calls if "texts" in call]
    assert len(encode_calls) == 3
    assert encode_calls[0]["max_seq_length"] == config.query_max_length
    assert encode_calls[1]["max_seq_length"] == config.document_max_length
    assert calls[0]["factory_kwargs"]["local_files_only"] is True
    assert calls[0]["factory_kwargs"]["model_kwargs"]["export"] is False
    await client.close()


@pytest.mark.asyncio
async def test_onnx_client_rejects_wrong_dimensions(tmp_path: Path) -> None:
    config = onnx_config(tmp_path, dimensions=3)
    write_manifest(config)
    client = OnnxEmbeddingClient(
        config,
        model_factory=lambda *args, **kwargs: FakeSentenceTransformer(2, []),
    )
    with pytest.raises(ModelEndpointError, match="expected 3 dimensions"):
        await client.embed_queries(["text"])
    await client.close()


@pytest.mark.asyncio
async def test_onnx_client_sanitizes_runtime_failure(tmp_path: Path) -> None:
    config = onnx_config(tmp_path)
    write_manifest(config)

    class BrokenModel(FakeSentenceTransformer):
        def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            raise RuntimeError("runtime internals")

    client = OnnxEmbeddingClient(
        config,
        model_factory=lambda *args, **kwargs: BrokenModel(3, []),
    )
    with pytest.raises(ModelEndpointError, match="inference failed"):
        await client.embed_queries(["text"])
    await client.close()


def test_onnx_profile_is_stable_and_missing_cache_is_explicit(tmp_path: Path) -> None:
    config = onnx_config(tmp_path)
    assert embedding_profile_id(config) == embedding_profile_id(config.model_copy())
    assert embedding_profile_id(config) != embedding_profile_id(
        config.model_copy(update={"optimization": "O2"})
    )
    client = OnnxEmbeddingClient(config)
    assert client.available() is False


@pytest.mark.asyncio
async def test_onnx_client_applies_explicit_pooling_override(tmp_path: Path) -> None:
    config = onnx_config(tmp_path).model_copy(update={"pooling": "mean"})
    write_manifest(config)

    class Pooling:
        pooling_mode_cls_token = True
        pooling_mode_mean_tokens = False
        pooling_mode_max_tokens = True

    model = FakeSentenceTransformer(3, [])
    model._modules = {"pooling": Pooling()}
    client = OnnxEmbeddingClient(config, model_factory=lambda *args, **kwargs: model)

    await client.embed_queries(["NR mobility"])

    assert model._modules["pooling"].pooling_mode_cls_token is False
    assert model._modules["pooling"].pooling_mode_mean_tokens is True
    assert model._modules["pooling"].pooling_mode_max_tokens is False
    await client.close()


def test_corrupt_or_unsafe_cached_onnx_artifact_is_unavailable(tmp_path: Path) -> None:
    config = onnx_config(tmp_path)
    write_manifest(config)
    artifact = embedding_profile_path(config) / "onnx" / "model.onnx"
    artifact.write_bytes(b"corrupt")
    assert OnnxEmbeddingClient(config).available() is False

    write_manifest(config)
    manifest_path = embedding_profile_path(config) / "threegpp-embedding-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["onnx_file"] = "../../outside.onnx"
    manifest_path.write_text(json.dumps(manifest))
    assert OnnxEmbeddingClient(config).available() is False


def test_model_preparation_pins_revision_exports_once_and_records_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threegpp_kg.models.onnx import prepare_onnx_model

    config = onnx_config(tmp_path)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    calls: list[dict[str, Any]] = []

    def snapshot_download(**kwargs: Any) -> str:
        calls.append(kwargs)
        return str(snapshot)

    class ExportModel:
        max_seq_length = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append({"model_args": args, "model_kwargs": kwargs})

        def save_pretrained(self, directory: Path) -> None:
            (directory / "onnx").mkdir(parents=True)
            (directory / "onnx" / "model.onnx").write_bytes(b"base")

    def export_optimized_onnx_model(
        model: Any, optimization: str, directory: str, *, file_suffix: str
    ) -> None:
        del model
        path = Path(directory) / "onnx" / f"model_{file_suffix}.onnx"
        path.write_bytes(optimization.encode())

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=snapshot_download),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(
            SentenceTransformer=ExportModel,
            export_optimized_onnx_model=export_optimized_onnx_model,
        ),
    )

    first = prepare_onnx_model(config)
    second = prepare_onnx_model(config)

    assert first == second
    assert calls[0]["revision"] == "a" * 40
    assert first["onnx_file"] == "onnx/model_O3.onnx"
    assert first["onnx_sha256"] == hashlib.sha256(b"O3").hexdigest()
    assert len([call for call in calls if "revision" in call]) == 1
