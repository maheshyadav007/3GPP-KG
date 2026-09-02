from __future__ import annotations

import json
import sys

import pytest

from threegpp_kg import cli


@pytest.mark.parametrize("command", ["check-config", "list-working-groups"])
def test_read_only_cli_commands_emit_json(monkeypatch, capsys, command: str) -> None:
    monkeypatch.setattr(sys, "argv", ["threegpp-kg", command])
    cli.main()
    assert json.loads(capsys.readouterr().out)


def test_validate_sources_cli_writes_result_and_exits_on_failure(monkeypatch, tmp_path) -> None:
    async def fake_validation():
        return {"passed": False, "working_groups": {}}

    output = tmp_path / "result.json"
    monkeypatch.setattr(cli, "validate_configured_sources", fake_validation)
    monkeypatch.setattr(sys, "argv", ["threegpp-kg", "validate-sources", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert json.loads(output.read_text())["passed"] is False


def test_embedding_backfill_cli_activates_and_writes_proof(monkeypatch, tmp_path) -> None:
    async def fake_backfill(settings, dataset_version: str, *, activate: bool):
        del settings
        return {"dataset_version": dataset_version, "activated": activate, "coverage": 1.0}

    output = tmp_path / "backfill.json"
    monkeypatch.setattr(cli, "run_embedding_backfill", fake_backfill)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threegpp-kg",
            "backfill-embeddings",
            "--dataset-version",
            "dataset-v1",
            "--activate",
            "--output",
            str(output),
        ],
    )

    cli.main()

    assert json.loads(output.read_text()) == {
        "activated": True,
        "coverage": 1.0,
        "dataset_version": "dataset-v1",
    }


def test_embedding_benchmark_cli_passes_sample_size(monkeypatch, capsys) -> None:
    async def fake_benchmark(settings, dataset_version: str, *, sample_size: int):
        del settings
        return {"dataset_version": dataset_version, "sample_size": sample_size}

    monkeypatch.setattr(cli, "benchmark_cached_embeddings", fake_benchmark)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threegpp-kg",
            "benchmark-embeddings",
            "--dataset-version",
            "dataset-v1",
            "--sample-size",
            "7",
        ],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "dataset_version": "dataset-v1",
        "sample_size": 7,
    }


def test_source_enrichment_cli_never_requests_tdoc_bodies(monkeypatch, capsys) -> None:
    async def fake_backfill(settings, group, request):
        del settings, group
        return {
            "dataset_version": request.dataset_version,
            "meeting_ids": request.meeting_ids,
            "document_limit": request.document_limit,
            "source_only": request.source_only,
        }

    monkeypatch.setattr(cli, "run_backfill", fake_backfill)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threegpp-kg",
            "enrich-meeting-sources",
            "--working-group",
            "RAN2",
            "--meeting",
            "132",
            "--dataset-version",
            "candidate-v1",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "dataset_version": "candidate-v1",
        "document_limit": 0,
        "meeting_ids": ["RAN2-132"],
        "source_only": True,
    }


def test_newsletter_build_and_review_cli(monkeypatch, tmp_path) -> None:
    async def fake_build(meeting, edition, *, render, last_k_meetings):
        return {
            "meeting": meeting,
            "edition": edition,
            "render": render,
            "last_k_meetings": last_k_meetings,
        }

    async def fake_review(newsletter_id, decision, reviewer, notes):
        return {
            "newsletter_id": newsletter_id,
            "decision": decision,
            "reviewer": reviewer,
            "notes": notes,
        }

    build_output = tmp_path / "newsletter.json"
    monkeypatch.setattr(cli, "_build_newsletter", fake_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threegpp-kg",
            "build-newsletter",
            "--meeting",
            "RAN2-133",
            "--edition",
            "final",
            "--last-k-meetings",
            "5",
            "--render",
            "--output",
            str(build_output),
        ],
    )
    cli.main()
    assert json.loads(build_output.read_text())["render"] is True

    review_output = tmp_path / "review.json"
    monkeypatch.setattr(cli, "_review_newsletter", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threegpp-kg",
            "review-newsletter",
            "--newsletter-id",
            "newsletter-1",
            "--decision",
            "approved",
            "--reviewer",
            "architect@example.com",
            "--notes",
            "verified",
            "--output",
            str(review_output),
        ],
    )
    cli.main()
    assert json.loads(review_output.read_text())["decision"] == "approved"
