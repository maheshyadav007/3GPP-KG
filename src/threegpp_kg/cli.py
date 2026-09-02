from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn

from .backfill import BackfillRequest, run_backfill
from .config import load_settings, load_working_groups
from .embedding_backfill import benchmark_cached_embeddings, run_embedding_backfill
from .graph_repair import rebuild_graph
from .local_ingest import ingest_local_manifests
from .mirror import benchmark_download_concurrency, mirror_working_group
from .models.client import create_model_client
from .publisher import activate_dataset, assess_activation_readiness
from .repository import SqlRepository
from .service import KnowledgeService
from .source_validation import validate_configured_sources, write_validation_result
from .storage.database import create_engine_and_session


def main() -> None:
    parser = argparse.ArgumentParser(prog="threegpp-kg")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="run the API and MCP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("check-config", help="validate and print redacted configuration")
    subparsers.add_parser("list-working-groups", help="list configured WG adapters")
    validate_sources = subparsers.add_parser(
        "validate-sources", help="validate configured fixtures against official listings"
    )
    validate_sources.add_argument("--output", type=Path)
    backfill = subparsers.add_parser(
        "backfill", help="ingest one or more meetings into an immutable candidate dataset"
    )
    backfill.add_argument("--working-group", required=True)
    meeting_selector = backfill.add_mutually_exclusive_group(required=True)
    meeting_selector.add_argument("--meeting", action="append")
    meeting_selector.add_argument("--last-k", type=int)
    backfill.add_argument("--dataset-version", required=True)
    backfill.add_argument(
        "--document-limit",
        type=int,
        default=0,
        help="TDoc bodies per meeting: 0 metadata only, -1 all, positive for a canary",
    )
    backfill.add_argument("--no-report", action="store_true")
    backfill.add_argument("--activate", action="store_true")
    backfill.add_argument("--output", type=Path)
    download_corpus = subparsers.add_parser(
        "download-corpus",
        help="download meeting artifacts to local immutable storage without parsing or a database",
    )
    download_corpus.add_argument("--working-group", required=True)
    download_corpus.add_argument("--meeting", action="append", required=True)
    download_corpus.add_argument("--manifest", type=Path, required=True)
    benchmark_downloads = subparsers.add_parser(
        "benchmark-downloads", help="compare 10 and 20 concurrent source downloads"
    )
    benchmark_downloads.add_argument("--working-group", required=True)
    benchmark_downloads.add_argument("--meeting", required=True)
    benchmark_downloads.add_argument("--sample-size", type=int, default=40)
    benchmark_downloads.add_argument("--output", type=Path)
    ingest_manifests = subparsers.add_parser(
        "ingest-manifests", help="ingest downloaded local manifests without source network access"
    )
    ingest_manifests.add_argument("--manifest", action="append", type=Path, required=True)
    ingest_manifests.add_argument("--dataset-version", required=True)
    ingest_manifests.add_argument("--meeting", action="append")
    ingest_manifests.add_argument("--output", type=Path)
    activate = subparsers.add_parser(
        "activate-dataset", help="validate and atomically activate an existing candidate dataset"
    )
    activate.add_argument("--dataset-version", required=True)
    activate.add_argument("--output", type=Path)
    rebuild = subparsers.add_parser(
        "rebuild-graph", help="rebuild an inactive dataset graph from canonical TDoc membership"
    )
    rebuild.add_argument("--dataset-version", required=True)
    rebuild.add_argument("--output", type=Path)
    embedding_backfill = subparsers.add_parser(
        "backfill-embeddings",
        help="download the configured ONNX model and resumably embed a dataset",
    )
    embedding_backfill.add_argument("--dataset-version", required=True)
    embedding_backfill.add_argument("--profile", choices=["default"], default="default")
    embedding_backfill.add_argument("--activate", action="store_true")
    embedding_backfill.add_argument("--output", type=Path)
    embedding_benchmark = subparsers.add_parser(
        "benchmark-embeddings", help="benchmark the cached ONNX model on dataset chunks"
    )
    embedding_benchmark.add_argument("--dataset-version", required=True)
    embedding_benchmark.add_argument("--sample-size", type=int, default=64)
    embedding_benchmark.add_argument("--output", type=Path)
    newsletter = subparsers.add_parser(
        "build-newsletter",
        help="build and persist an evidence packet, optionally rendering analytical prose",
    )
    newsletter.add_argument("--meeting", required=True)
    newsletter.add_argument("--edition", choices=["provisional", "final"], default="provisional")
    newsletter.add_argument("--last-k-meetings", type=int)
    newsletter.add_argument("--render", action="store_true")
    newsletter.add_argument("--output", type=Path)
    review = subparsers.add_parser(
        "review-newsletter", help="approve or reject a rendered immutable newsletter"
    )
    review.add_argument("--newsletter-id", required=True)
    review.add_argument("--decision", choices=["approved", "rejected"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", default="")
    review.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "serve":
        uvicorn.run("threegpp_kg.api:app", host=args.host, port=args.port)
    elif args.command == "check-config":
        print(json.dumps(load_settings().redacted(), indent=2, sort_keys=True))
    elif args.command == "list-working-groups":
        print(
            json.dumps(
                {key: value.model_dump() for key, value in load_working_groups().items()}, indent=2
            )
        )
    elif args.command == "validate-sources":
        result = asyncio.run(validate_configured_sources())
        if args.output:
            write_validation_result(result, args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["passed"]:
            raise SystemExit(1)
    elif args.command == "backfill":
        settings = load_settings()
        groups = load_working_groups()
        working_group = args.working_group.upper()
        if working_group not in groups:
            raise SystemExit(f"unknown working group {working_group}")
        meeting_ids = [
            value.upper() if "-" in value else f"{working_group}-{value}"
            for value in (args.meeting or [])
        ]
        result = asyncio.run(
            run_backfill(
                settings,
                groups[working_group],
                BackfillRequest(
                    working_group=working_group,
                    meeting_ids=meeting_ids,
                    dataset_version=args.dataset_version,
                    last_k_meetings=args.last_k,
                    document_limit=args.document_limit,
                    include_report=not args.no_report,
                    activate=args.activate,
                ),
            )
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "download-corpus":
        settings = load_settings()
        groups = load_working_groups()
        working_group = args.working_group.upper()
        if working_group not in groups:
            raise SystemExit(f"unknown working group {working_group}")
        meeting_ids = [
            value.upper() if "-" in value else f"{working_group}-{value}" for value in args.meeting
        ]
        result = asyncio.run(
            mirror_working_group(
                settings,
                groups[working_group],
                meeting_ids,
                args.manifest,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "benchmark-downloads":
        settings = load_settings()
        groups = load_working_groups()
        working_group = args.working_group.upper()
        if working_group not in groups:
            raise SystemExit(f"unknown working group {working_group}")
        meeting_id = (
            args.meeting.upper() if "-" in args.meeting else f"{working_group}-{args.meeting}"
        )
        result = asyncio.run(
            benchmark_download_concurrency(
                settings,
                groups[working_group],
                meeting_id,
                args.sample_size,
            )
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "ingest-manifests":
        result = asyncio.run(
            ingest_local_manifests(
                load_settings(),
                args.manifest,
                args.dataset_version,
                {value.casefold() for value in args.meeting} if args.meeting else None,
            )
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "activate-dataset":
        result = asyncio.run(_activate_existing_dataset(args.dataset_version))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["activated"]:
            raise SystemExit(1)
    elif args.command == "rebuild-graph":
        result = asyncio.run(_rebuild_existing_graph(args.dataset_version))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "backfill-embeddings":
        result = asyncio.run(
            run_embedding_backfill(load_settings(), args.dataset_version, activate=args.activate)
        )
        _write_optional_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "benchmark-embeddings":
        result = asyncio.run(
            benchmark_cached_embeddings(
                load_settings(), args.dataset_version, sample_size=args.sample_size
            )
        )
        _write_optional_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "build-newsletter":
        result = asyncio.run(
            _build_newsletter(
                args.meeting,
                args.edition,
                render=args.render,
                last_k_meetings=args.last_k_meetings,
            )
        )
        _write_optional_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "review-newsletter":
        result = asyncio.run(
            _review_newsletter(
                args.newsletter_id,
                args.decision,
                args.reviewer,
                args.notes,
            )
        )
        _write_optional_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))


def _write_optional_json(output: Path | None, result: dict[str, object]) -> None:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def _activate_existing_dataset(dataset_version_id: str) -> dict[str, object]:
    settings = load_settings()
    if settings.database.mode != "sql" or not settings.database.url.startswith("postgresql+"):
        raise ValueError("dataset activation requires database.mode=sql with PostgreSQL")
    engine, sessions = create_engine_and_session(settings.database)
    try:
        async with sessions() as session:
            assessment = await assess_activation_readiness(session, dataset_version_id)
            if not assessment["ready"]:
                return {**assessment, "activated": False}
            await activate_dataset(session, dataset_version_id)
            await session.commit()
            return {**assessment, "activated": True}
    finally:
        await engine.dispose()


async def _rebuild_existing_graph(dataset_version_id: str) -> dict[str, object]:
    settings = load_settings()
    if settings.database.mode != "sql" or not settings.database.url.startswith("postgresql+"):
        raise ValueError("graph rebuilding requires database.mode=sql with PostgreSQL")
    engine, sessions = create_engine_and_session(settings.database)
    try:
        async with sessions() as session:
            result = await rebuild_graph(session, dataset_version_id)
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _build_newsletter(
    meeting_id: str,
    edition: str,
    *,
    render: bool,
    last_k_meetings: int | None,
) -> dict[str, object]:
    settings = load_settings()
    if settings.database.mode != "sql":
        raise ValueError("newsletter persistence requires database.mode=sql")
    if render and not settings.features.newsletter_generation_enabled:
        raise ValueError("newsletter rendering is disabled by configuration")
    newsletter_config = settings.newsletter.model_copy(
        update=({"last_k_meetings": last_k_meetings} if last_k_meetings is not None else {})
    )
    generation_client = (
        create_model_client(
            settings.models.generation,
            timeout_seconds=settings.models.timeout_seconds,
            retries=settings.models.retries,
        )
        if render
        else None
    )
    engine, sessions = create_engine_and_session(settings.database)
    service = KnowledgeService(
        SqlRepository(
            sessions,
            settings.retrieval,
            preview_dataset_version=settings.database.preview_dataset_version,
        ),
        newsletter_config=newsletter_config,
        generation_client=generation_client,
        features=settings.features,
    )
    try:
        result = await service.build_newsletter(meeting_id, edition, render=render)
        return result.model_dump(mode="json")
    finally:
        await service.close_models()
        await engine.dispose()


async def _review_newsletter(
    newsletter_id: str,
    decision: str,
    reviewer: str,
    notes: str,
) -> dict[str, object]:
    settings = load_settings()
    if settings.database.mode != "sql":
        raise ValueError("newsletter review requires database.mode=sql")
    engine, sessions = create_engine_and_session(settings.database)
    service = KnowledgeService(
        SqlRepository(
            sessions,
            settings.retrieval,
            preview_dataset_version=settings.database.preview_dataset_version,
        ),
        newsletter_config=settings.newsletter,
        features=settings.features,
    )
    try:
        result = await service.review_newsletter(newsletter_id, decision, reviewer, notes)
        return result.model_dump(mode="json")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
