from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from threegpp_kg.config import DatabaseConfig
from threegpp_kg.domain import Meeting
from threegpp_kg.scheduler import (
    Job,
    JobWorker,
    complete_job,
    enqueue_job,
    enqueue_newsletter_jobs,
    fail_job,
    finish_leased_job,
    lease_job,
    lease_next_job,
    retry_leased_job,
)
from threegpp_kg.storage.database import Base, JobRow, create_engine_and_session


def test_job_lease_and_completion() -> None:
    now = datetime.now(UTC)
    leased = lease_job(Job("job-1", "meeting:1"), "worker-a", now, 60)
    assert leased.state == "running" and leased.leased_until == now + timedelta(seconds=60)
    complete = complete_job(leased, "worker-a")
    assert complete.state == "complete" and complete.leased_by is None


def test_active_lease_cannot_be_stolen() -> None:
    now = datetime.now(UTC)
    leased = lease_job(Job("job-1", "meeting:1"), "worker-a", now, 60)
    with pytest.raises(ValueError, match="active lease"):
        lease_job(leased, "worker-b", now + timedelta(seconds=10), 60)


def test_failures_backoff_then_dead_letter() -> None:
    now = datetime.now(UTC)
    first = lease_job(Job("job-1", "meeting:1"), "worker", now, 60)
    retry = fail_job(first, "worker", "network", now, max_attempts=2)
    assert retry.state == "pending" and retry.attempts == 1 and retry.available_at > now
    second = lease_job(retry, "worker", retry.available_at, 60)
    dead = fail_job(second, "worker", "network", retry.available_at, max_attempts=2)
    assert dead.state == "dead_letter" and dead.attempts == 2


@pytest.mark.asyncio
async def test_database_jobs_are_idempotent_reclaim_expired_leases_and_dead_letter(
    tmp_path,
) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/jobs.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with sessions() as session:
        first = await enqueue_job(
            session,
            job_id="job-1",
            job_type="discover",
            idempotency_key="RAN2:listing:v1",
            payload={"wg": "RAN2"},
            available_at=now,
        )
        duplicate = await enqueue_job(
            session,
            job_id="job-2",
            job_type="discover",
            idempotency_key="RAN2:listing:v1",
            payload={"wg": "RAN2"},
            available_at=now,
        )
        assert duplicate.id == first.id
        leased = await lease_next_job(session, worker_id="worker-a", lease_seconds=30, now=now)
        assert leased and leased.id == "job-1"
        assert (
            await lease_next_job(session, worker_id="worker-b", lease_seconds=30, now=now) is None
        )
        reclaimed = await lease_next_job(
            session,
            worker_id="worker-b",
            lease_seconds=30,
            now=now + timedelta(seconds=31),
        )
        assert reclaimed and reclaimed.leased_by == "worker-b"
        await retry_leased_job(
            session,
            reclaimed,
            worker_id="worker-b",
            error="source unavailable",
            max_attempts=1,
            now=now + timedelta(seconds=31),
        )
        assert reclaimed.state == "dead_letter"
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_delayed_final_report_enqueues_new_immutable_edition(tmp_path) -> None:
    database = tmp_path / "scheduler-newsletter.db"
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{database}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    provisional = Meeting(
        id="RAN2-133",
        working_group_id="RAN2",
        number=133,
        name="RAN2 #133",
        source_url="https://www.3gpp.org/133",
        readiness="provisional_ready",
    )
    async with sessions() as session, session.begin():
        first = await enqueue_newsletter_jobs(
            session, dataset_version="dataset-v1", meeting=provisional
        )
        duplicate = await enqueue_newsletter_jobs(
            session, dataset_version="dataset-v1", meeting=provisional
        )
    assert [item.payload["edition"] for item in first] == ["provisional"]
    assert duplicate[0].id == first[0].id

    final = provisional.model_copy(update={"readiness": "final_ready"})
    async with sessions() as session, session.begin():
        editions = await enqueue_newsletter_jobs(
            session, dataset_version="dataset-v1", meeting=final
        )
    assert [item.payload["edition"] for item in editions] == ["provisional", "final"]
    assert editions[0].id == first[0].id
    assert editions[1].id != first[0].id
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_job_completion_checks_owner(tmp_path) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/complete.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with sessions() as session:
        await enqueue_job(
            session,
            job_id="job-1",
            job_type="publish",
            idempotency_key="publish:v1",
            payload={},
            available_at=now,
        )
        row = await lease_next_job(session, worker_id="worker", lease_seconds=30, now=now)
        assert row
        with pytest.raises(ValueError, match="does not own"):
            await finish_leased_job(session, row, "other")
        await finish_leased_job(session, row, "worker")
        assert row.state == "complete"
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_worker_commits_success_and_dead_letters_handler_failure(tmp_path) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/worker.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        await enqueue_job(
            session,
            job_id="job-ok",
            job_type="ok",
            idempotency_key="ok:1",
            payload={"meeting": "RAN2-133"},
            available_at=now,
        )
        await enqueue_job(
            session,
            job_id="job-fail",
            job_type="fail",
            idempotency_key="fail:1",
            payload={},
            available_at=now,
        )

    handled: list[str] = []

    async def ok(payload) -> None:
        handled.append(payload["meeting"])

    async def fail(payload) -> None:
        del payload
        raise RuntimeError("handler failed")

    worker = JobWorker(
        sessions,
        worker_id="worker-1",
        handlers={"ok": ok, "fail": fail},
        lease_seconds=30,
        max_attempts=1,
    )
    assert await worker.run_once(now)
    assert await worker.run_once(now)
    assert not await worker.run_once(now)
    assert handled == ["RAN2-133"]
    async with sessions() as session:
        rows = list((await session.scalars(select(JobRow).order_by(JobRow.id))).all())
    assert [(row.id, row.state) for row in rows] == [
        ("job-fail", "dead_letter"),
        ("job-ok", "complete"),
    ]
    assert "handler failed" in (rows[0].last_error or "")
    await engine.dispose()
