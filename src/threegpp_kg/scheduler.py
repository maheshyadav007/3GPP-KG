from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .constants import NEWSLETTER_PACKET_VERSION, MeetingReadiness
from .domain import Meeting
from .storage.database import JobRow

TERMINAL_JOB_STATES = frozenset({"complete", "dead_letter"})
JobHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    idempotency_key: str
    state: str = "pending"
    attempts: int = 0
    available_at: datetime = datetime.min.replace(tzinfo=UTC)
    leased_until: datetime | None = None
    leased_by: str | None = None
    last_error: str | None = None


def lease_job(job: Job, worker_id: str, now: datetime, lease_seconds: int) -> Job:
    if job.state in TERMINAL_JOB_STATES:
        raise ValueError("terminal jobs cannot be leased")
    if job.available_at > now:
        raise ValueError("job is not yet available")
    if job.leased_until and job.leased_until > now:
        raise ValueError("job already has an active lease")
    return replace(
        job,
        state="running",
        leased_by=worker_id,
        leased_until=now + timedelta(seconds=lease_seconds),
    )


def complete_job(job: Job, worker_id: str) -> Job:
    _assert_owner(job, worker_id)
    return replace(job, state="complete", leased_by=None, leased_until=None)


def fail_job(job: Job, worker_id: str, error: str, now: datetime, max_attempts: int) -> Job:
    _assert_owner(job, worker_id)
    attempts = job.attempts + 1
    if attempts >= max_attempts:
        return replace(
            job,
            state="dead_letter",
            attempts=attempts,
            last_error=error,
            leased_by=None,
            leased_until=None,
        )
    delay = min(3600, 2**attempts * 30)
    return replace(
        job,
        state="pending",
        attempts=attempts,
        available_at=now + timedelta(seconds=delay),
        last_error=error,
        leased_by=None,
        leased_until=None,
    )


def _assert_owner(job: Job, worker_id: str) -> None:
    if job.state != "running" or job.leased_by != worker_id:
        raise ValueError("worker does not own the active job lease")


async def enqueue_job(
    session: AsyncSession,
    *,
    job_id: str,
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    available_at: datetime | None = None,
) -> JobRow:
    existing = await session.scalar(select(JobRow).where(JobRow.idempotency_key == idempotency_key))
    if existing:
        return existing
    row = JobRow(
        id=job_id,
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload=payload,
        state="pending",
        attempts=0,
        available_at=available_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def enqueue_newsletter_jobs(
    session: AsyncSession,
    *,
    dataset_version: str,
    meeting: Meeting,
) -> list[JobRow]:
    readiness = MeetingReadiness(meeting.readiness)
    if readiness not in {
        MeetingReadiness.PROVISIONAL_READY,
        MeetingReadiness.FINAL_READY,
    }:
        return []
    editions = ["provisional"]
    if readiness == MeetingReadiness.FINAL_READY:
        editions.append("final")
    rows = []
    for edition in editions:
        key = f"newsletter:{NEWSLETTER_PACKET_VERSION}:{dataset_version}:{meeting.id}:{edition}"
        rows.append(
            await enqueue_job(
                session,
                job_id=key,
                job_type="build_newsletter",
                idempotency_key=key,
                payload={
                    "dataset_version": dataset_version,
                    "meeting_id": meeting.id,
                    "edition": edition,
                    "render": False,
                },
            )
        )
    return rows


async def lease_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> JobRow | None:
    instant = now or datetime.now(UTC)
    row = await session.scalar(
        select(JobRow)
        .where(
            JobRow.available_at <= instant,
            or_(
                JobRow.state == "pending",
                and_(JobRow.state == "running", JobRow.leased_until <= instant),
            ),
        )
        .order_by(JobRow.available_at, JobRow.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.state = "running"
    row.leased_by = worker_id
    row.leased_until = instant + timedelta(seconds=lease_seconds)
    await session.flush()
    return row


async def finish_leased_job(session: AsyncSession, row: JobRow, worker_id: str) -> None:
    if row.state != "running" or row.leased_by != worker_id:
        raise ValueError("worker does not own the active job lease")
    row.state = "complete"
    row.leased_by = None
    row.leased_until = None
    await session.flush()


async def retry_leased_job(
    session: AsyncSession,
    row: JobRow,
    *,
    worker_id: str,
    error: str,
    max_attempts: int,
    now: datetime | None = None,
) -> None:
    if row.state != "running" or row.leased_by != worker_id:
        raise ValueError("worker does not own the active job lease")
    instant = now or datetime.now(UTC)
    row.attempts += 1
    row.last_error = error[:4000]
    row.leased_by = None
    row.leased_until = None
    if row.attempts >= max_attempts:
        row.state = "dead_letter"
    else:
        row.state = "pending"
        row.available_at = instant + timedelta(seconds=min(3600, 2**row.attempts * 30))
    await session.flush()


class JobWorker:
    """Runs one durable job while keeping lease state in short transactions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        handlers: dict[str, JobHandler],
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        self.sessions = sessions
        self.worker_id = worker_id
        self.handlers = handlers
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def run_once(self, now: datetime | None = None) -> bool:
        async with self.sessions() as session, session.begin():
            row = await lease_next_job(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                now=now,
            )
            if row is None:
                return False
            job_id = row.id
            job_type = row.job_type
            payload = dict(row.payload)

        handler = self.handlers.get(job_type)
        try:
            if handler is None:
                raise ValueError(f"no handler is registered for job type {job_type!r}")
            await handler(payload)
        except Exception as exc:
            async with self.sessions() as session, session.begin():
                leased = await session.get(JobRow, job_id, with_for_update=True)
                if leased is None:
                    raise RuntimeError(f"leased job {job_id!r} disappeared") from exc
                await retry_leased_job(
                    session,
                    leased,
                    worker_id=self.worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self.max_attempts,
                    now=now,
                )
            return True

        async with self.sessions() as session, session.begin():
            leased = await session.get(JobRow, job_id, with_for_update=True)
            if leased is None:
                raise RuntimeError(f"leased job {job_id!r} disappeared")
            await finish_leased_job(session, leased, self.worker_id)
        return True
