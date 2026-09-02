from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from threegpp_kg.config import (
    ModelEndpointConfig,
    load_organization_aliases,
    load_settings,
    load_working_groups,
)


def test_onnx_revision_must_be_an_immutable_commit() -> None:
    with pytest.raises(ValidationError, match="immutable commit SHA"):
        ModelEndpointConfig(
            provider="onnx",
            model="test/model",
            revision="main",
            dimensions=768,
        )


def test_default_configuration_loads() -> None:
    settings = load_settings()
    assert settings.ingestion.history_months == 24
    assert settings.features.newsletter_generation_enabled is False
    assert settings.database.mode == "fixture"
    assert settings.chunking.min_tokens <= settings.chunking.target_tokens
    assert settings.evidence_blocks.target_tokens > settings.chunking.target_tokens
    assert settings.redacted()["database"]["url"].startswith("sqlite")
    assert settings.models.embedding.provider == "onnx"
    assert settings.models.embedding.model == "ibm-granite/granite-embedding-english-r2"
    assert settings.models.embedding.dimensions == 768
    assert settings.models.embedding.revision
    assert settings.models.generation.model == "Qwen/Qwen3-32B"
    assert settings.newsletter.last_k_meetings == 5


def test_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    load_settings.cache_clear()
    monkeypatch.setenv("THREEGPP_EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setenv("THREEGPP_EMBEDDING_QUERY_MAX_LENGTH", "384")
    monkeypatch.setenv("THREEGPP_TOPIC_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("THREEGPP_HTTP_REQUESTS_PER_SECOND", "4.5")
    monkeypatch.setenv("THREEGPP_HTTP_MAX_CONCURRENCY", "8")
    monkeypatch.setenv("THREEGPP_PARSER_DOCUMENT_WORKERS", "6")
    monkeypatch.setenv("THREEGPP_DATABASE_PREVIEW_DATASET_VERSION", "candidate-v1")
    monkeypatch.setenv("THREEGPP_NEWSLETTER_LAST_K_MEETINGS", "7")
    settings = load_settings()
    assert settings.models.embedding.dimensions == 1536
    assert settings.models.embedding.query_max_length == 384
    assert settings.features.topic_extraction_enabled is True
    assert settings.http.requests_per_second == 4.5
    assert settings.http.max_concurrency == 8
    assert settings.parsers.document_workers == 6
    assert settings.database.preview_dataset_version == "candidate-v1"
    assert settings.newsletter.last_k_meetings == 7
    load_settings.cache_clear()


def test_invalid_chunk_order_is_rejected(tmp_path: Path) -> None:
    source = yaml.safe_load((Path("config/defaults.yaml")).read_text())
    source["chunking"].update(min_tokens=800, target_tokens=500, max_tokens=700)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(source))
    load_settings.cache_clear()
    with pytest.raises(ValidationError, match="min <= target <= max"):
        load_settings(path)
    load_settings.cache_clear()


def test_invalid_evidence_block_order_is_rejected(tmp_path: Path) -> None:
    source = yaml.safe_load((Path("config/defaults.yaml")).read_text())
    source["evidence_blocks"].update(target_tokens=1500, max_tokens=1000)
    path = tmp_path / "invalid-evidence.yaml"
    path.write_text(yaml.safe_dump(source))
    load_settings.cache_clear()
    with pytest.raises(ValidationError, match="target <= max"):
        load_settings(path)
    load_settings.cache_clear()


def test_working_groups_are_data_driven() -> None:
    groups = load_working_groups()
    assert set(groups) == {"RAN2", "RAN3", "SA2", "CT1"}
    assert groups["SA2"].tdoc_prefix == "S2"
    assert groups["CT1"].tdoc_prefix == "C1"


def test_production_configuration_requires_database_and_auth_but_allows_local_storage(
    tmp_path,
) -> None:
    source = yaml.safe_load(Path("config/defaults.yaml").read_text())
    source["app"]["environment"] = "production"
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(source))
    load_settings.cache_clear()
    with pytest.raises(ValidationError, match="PostgreSQL.*OIDC"):
        load_settings(path)
    load_settings.cache_clear()

    source["database"]["url"] = "postgresql+asyncpg://localhost/threegpp"
    source["database"]["mode"] = "sql"
    source["security"].update(auth_required=True, oidc_issuer="https://identity.example")
    path.write_text(yaml.safe_dump(source))
    settings = load_settings(path)
    assert settings.object_store.backend == "local"
    assert settings.models.embedding.base_url is None
    load_settings.cache_clear()

    source["database"]["preview_dataset_version"] = "building-v1"
    path.write_text(yaml.safe_dump(source))
    with pytest.raises(ValidationError, match="cannot read a building preview"):
        load_settings(path)
    load_settings.cache_clear()


def test_organization_alias_configuration_rejects_conflicts_and_bad_shapes(tmp_path) -> None:
    load_organization_aliases.cache_clear()
    valid = tmp_path / "valid.yaml"
    valid.write_text("Qualcomm:\n  - Qualcomm Incorporated\n")
    assert load_organization_aliases(valid)["qualcomm incorporated"] == "Qualcomm"
    load_organization_aliases.cache_clear()

    conflict = tmp_path / "conflict.yaml"
    conflict.write_text("One: [Alias]\nTwo: [Alias]\n")
    with pytest.raises(ValueError, match="multiple names"):
        load_organization_aliases(conflict)
    load_organization_aliases.cache_clear()

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_organization_aliases(malformed)
    load_organization_aliases.cache_clear()
