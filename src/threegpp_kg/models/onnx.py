from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import ModelEndpointConfig
from .client import ModelEndpointError

ModelFactory = Callable[..., Any]


def embedding_profile_id(config: ModelEndpointConfig) -> str:
    semantic_config = {
        "provider": config.provider,
        "model": config.model,
        "revision": config.revision,
        "dimensions": config.dimensions,
        "onnx_file": config.onnx_file,
        "optimization": config.optimization,
        "execution_provider": config.execution_provider,
        "document_max_length": config.document_max_length,
        "query_max_length": config.query_max_length,
        "pooling": config.pooling,
        "normalize": config.normalize,
        "query_prompt": config.query_prompt,
        "document_prompt": config.document_prompt,
    }
    digest = hashlib.sha256(
        json.dumps(semantic_config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"emb-{digest[:32]}"


def embedding_profile_path(config: ModelEndpointConfig) -> Path:
    return config.cache_dir.expanduser().resolve() / "profiles" / embedding_profile_id(config)


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_manifest(config: ModelEndpointConfig, directory: Path) -> dict[str, Any]:
    onnx_files = sorted(directory.rglob("*.onnx"))
    if not onnx_files:
        raise ModelEndpointError("exported model does not contain an ONNX artifact")
    selected = _select_onnx_file(config, directory, onnx_files)
    return {
        "profile_id": embedding_profile_id(config),
        "provider": "onnx",
        "model": config.model,
        "revision": config.revision,
        "dimensions": config.dimensions,
        "pooling": config.pooling,
        "normalize": config.normalize,
        "query_prompt": config.query_prompt,
        "document_prompt": config.document_prompt,
        "document_max_length": config.document_max_length,
        "query_max_length": config.query_max_length,
        "onnx_file": str(selected.relative_to(directory)),
        "onnx_sha256": _file_sha256(selected),
        "runtime_version": _package_version("onnxruntime"),
        "sentence_transformers_version": _package_version("sentence-transformers"),
    }


def _select_onnx_file(config: ModelEndpointConfig, directory: Path, candidates: list[Path]) -> Path:
    if config.onnx_file:
        selected = directory / config.onnx_file
        if not selected.is_file():
            raise ModelEndpointError(f"configured ONNX file does not exist: {config.onnx_file}")
        return selected
    optimized = [path for path in candidates if f"_{config.optimization}" in path.stem]
    return optimized[0] if optimized else candidates[0]


def read_model_manifest(
    config: ModelEndpointConfig, *, verify_checksum: bool = True
) -> dict[str, Any]:
    profile_path = embedding_profile_path(config)
    manifest_path = profile_path / "threegpp-embedding-manifest.json"
    if not manifest_path.is_file():
        raise ModelEndpointError(
            "cached ONNX model is missing; run backfill-embeddings with downloads enabled"
        )
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelEndpointError("cached ONNX model manifest is invalid") from exc
    if not isinstance(parsed, dict):
        raise ModelEndpointError("cached ONNX model manifest must be an object")
    manifest: dict[str, Any] = parsed
    if manifest.get("profile_id") != embedding_profile_id(config):
        raise ModelEndpointError("cached ONNX model does not match configured profile")
    onnx_file = manifest.get("onnx_file")
    checksum = manifest.get("onnx_sha256")
    if not isinstance(onnx_file, str) or not isinstance(checksum, str):
        raise ModelEndpointError("cached ONNX model manifest is missing artifact metadata")
    artifact = (profile_path / onnx_file).resolve()
    if profile_path not in artifact.parents or not artifact.is_file():
        raise ModelEndpointError("cached ONNX artifact is missing or outside its profile")
    if verify_checksum and _file_sha256(artifact) != checksum:
        raise ModelEndpointError("cached ONNX artifact checksum does not match its manifest")
    return manifest


def prepare_onnx_model(config: ModelEndpointConfig) -> dict[str, Any]:
    if config.provider != "onnx":
        raise ModelEndpointError("model preparation requires the ONNX provider")
    target = embedding_profile_path(config)
    if (target / "threegpp-embedding-manifest.json").is_file():
        return read_model_manifest(config)
    if not config.download_on_backfill:
        raise ModelEndpointError("ONNX model download is disabled")
    try:
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer, export_optimized_onnx_model
    except ImportError as exc:
        raise ModelEndpointError(
            "ONNX dependencies are missing; run `uv sync --extra onnx`"
        ) from exc
    cache_root = config.cache_dir.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_download(
        repo_id=str(config.model),
        revision=config.revision,
        cache_dir=cache_root / "hub",
        local_files_only=False,
    )
    temporary = Path(tempfile.mkdtemp(prefix="embedding-export-", dir=cache_root))
    try:
        model = SentenceTransformer(
            snapshot,
            backend="onnx",
            local_files_only=True,
            model_kwargs={"provider": config.execution_provider, "export": True},
        )
        model.max_seq_length = config.document_max_length
        model.save_pretrained(temporary)
        if config.optimization != "none":
            export_optimized_onnx_model(
                model,
                config.optimization,
                str(temporary),
                file_suffix=config.optimization,
            )
        manifest = _model_manifest(config, temporary)
        (temporary / "threegpp-embedding-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(temporary, target)
        except OSError:
            if not target.is_dir():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
        return read_model_manifest(config)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


class OnnxEmbeddingClient:
    def __init__(
        self,
        config: ModelEndpointConfig,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        if config.provider != "onnx" or not config.model or not config.revision:
            raise ModelEndpointError("a pinned ONNX embedding model must be configured")
        if not config.dimensions:
            raise ModelEndpointError("ONNX embedding dimensions must be configured")
        self.config = config
        self.profile_id = embedding_profile_id(config)
        self.model_name = config.model
        self.revision = config.revision
        self.dimensions = config.dimensions
        self._model_factory = model_factory
        self._model: Any | None = None
        self._model_lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrency,
            thread_name_prefix="onnx-embedding",
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_lock = Lock()
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            read_model_manifest(self.config)
        except ModelEndpointError:
            self._available = False
        else:
            self._available = True
        return self._available

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            manifest = read_model_manifest(self.config, verify_checksum=self._available is not True)
            factory = self._model_factory
            session_options = None
            if factory is None:
                try:
                    import onnxruntime as ort
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise ModelEndpointError(
                        "ONNX dependencies are missing; run `uv sync --extra onnx`"
                    ) from exc
                factory = SentenceTransformer
                session_options = ort.SessionOptions()
                session_options.intra_op_num_threads = self.config.intra_op_threads
                session_options.inter_op_num_threads = self.config.inter_op_threads
            model_kwargs: dict[str, Any] = {
                "provider": self.config.execution_provider,
                "file_name": manifest["onnx_file"],
                "export": False,
            }
            if session_options is not None:
                model_kwargs["session_options"] = session_options
            self._model = factory(
                str(embedding_profile_path(self.config)),
                backend="onnx",
                local_files_only=self.config.runtime_local_files_only,
                model_kwargs=model_kwargs,
            )
            self._apply_pooling_override(self._model)
        return self._model

    def _apply_pooling_override(self, model: Any) -> None:
        if self.config.pooling == "auto":
            return
        modules = getattr(model, "_modules", {}).values()
        pooling = next(
            (module for module in modules if module.__class__.__name__ == "Pooling"), None
        )
        if pooling is None:
            raise ModelEndpointError("configured pooling override cannot be applied")
        pooling.pooling_mode_cls_token = self.config.pooling == "cls"
        pooling.pooling_mode_mean_tokens = self.config.pooling == "mean"
        for attribute in (
            "pooling_mode_max_tokens",
            "pooling_mode_mean_sqrt_len_tokens",
            "pooling_mode_weightedmean_tokens",
            "pooling_mode_lasttoken",
        ):
            if hasattr(pooling, attribute):
                setattr(pooling, attribute, False)

    async def warmup(self) -> None:
        await self.embed_queries(["3GPP semantic search warmup"])

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_queries(texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) == 1 and self.config.query_cache_size:
            key = texts[0]
            with self._cache_lock:
                cached = self._query_cache.get(key)
                if cached is not None:
                    self._query_cache.move_to_end(key)
                    return [cached.copy()]
            vectors = await self._encode(
                texts, self.config.query_max_length, self.config.query_prompt
            )
            with self._cache_lock:
                self._query_cache[key] = vectors[0].copy()
                self._query_cache.move_to_end(key)
                while len(self._query_cache) > self.config.query_cache_size:
                    self._query_cache.popitem(last=False)
            return vectors
        return await self._encode(texts, self.config.query_max_length, self.config.query_prompt)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._encode(
            texts, self.config.document_max_length, self.config.document_prompt
        )

    async def _encode(self, texts: list[str], max_length: int, prompt: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.config.batch_size):
            batch = texts[start : start + self.config.batch_size]
            async with self._semaphore:
                loop = asyncio.get_running_loop()
                try:
                    encoded = await loop.run_in_executor(
                        self._executor, self._encode_sync, batch, max_length, prompt
                    )
                except ModelEndpointError:
                    raise
                except Exception as exc:
                    raise ModelEndpointError("ONNX embedding inference failed") from exc
            vectors.extend(encoded)
        return vectors

    def _encode_sync(self, texts: list[str], max_length: int, prompt: str) -> list[list[float]]:
        model = self._load()
        model.max_seq_length = max_length
        result = model.encode(
            texts,
            batch_size=min(self.config.batch_size, len(texts)),
            normalize_embeddings=self.config.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
            prompt=prompt or None,
        )
        vectors = [[float(value) for value in row] for row in result]
        self._validate_vectors(vectors, len(texts))
        return vectors

    def _validate_vectors(self, vectors: list[list[float]], expected_count: int) -> None:
        if len(vectors) != expected_count:
            raise ModelEndpointError("ONNX embedding result does not match the request batch")
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise ModelEndpointError(
                    f"expected {self.dimensions} dimensions, received {len(vector)}"
                )
            if not all(math.isfinite(value) for value in vector):
                raise ModelEndpointError("ONNX embedding contains non-finite values")
            if self.config.normalize:
                norm = math.sqrt(sum(value * value for value in vector))
                if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
                    raise ModelEndpointError("ONNX embedding is not L2-normalized")
