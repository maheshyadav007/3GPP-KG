from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from threegpp_kg.config import DatabaseConfig
from threegpp_kg.constants import DatasetState
from threegpp_kg.publisher import activate_dataset, assess_activation_readiness
from threegpp_kg.storage.database import (
    ArtifactVersionRow,
    Base,
    DatasetVersionRow,
    DocumentBlockRow,
    MeetingRow,
    TDocRow,
    create_engine_and_session,
)


@pytest.mark.asyncio
async def test_activation_readiness_requires_evidence_complete_dataset(tmp_path) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/readiness.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            DatasetVersionRow(
                id="candidate",
                state=DatasetState.VALIDATED,
                created_at=datetime.now(UTC),
                is_active=False,
                stats={},
            )
        )
        session.add(
            MeetingRow(
                id="RAN2-134",
                dataset_version_id="candidate",
                working_group_id="RAN2",
                number=134,
                variant="",
                name="RAN2 #134",
                source_url="https://www.3gpp.org/meeting/",
                starts_on=date(2026, 5, 18),
                ends_on=date(2026, 5, 22),
                readiness="final_ready",
            )
        )
        session.add(
            TDocRow(
                id="R2-2600001",
                dataset_version_id="candidate",
                meeting_id="RAN2-134",
                title="Carrier aggregation",
                status="agreed",
            )
        )
        session.add_all(
            [
                ArtifactVersionRow(
                    id="report-artifact",
                    dataset_version_id="candidate",
                    meeting_id="RAN2-134",
                    kind="report",
                    source_url="https://www.3gpp.org/report.zip",
                    filename="report.zip",
                    sha256="a" * 64,
                    content_type="application/zip",
                    object_key="sha256/aa/report",
                    parse_status="parsed",
                ),
                ArtifactVersionRow(
                    id="tdoc-artifact",
                    dataset_version_id="candidate",
                    meeting_id="RAN2-134",
                    kind="tdoc",
                    source_url="https://www.3gpp.org/R2-2600001.zip",
                    filename="R2-2600001.zip",
                    sha256="b" * 64,
                    content_type="application/zip",
                    object_key="sha256/bb/tdoc",
                    parse_status="parsed",
                ),
            ]
        )
        session.add(
            DocumentBlockRow(
                id="R2-2600001:block:0",
                dataset_version_id="candidate",
                document_id="R2-2600001",
                block_index=0,
                kind="paragraph",
                text="Agreement: adopt the proposal.",
                section_path=[],
            )
        )
        await session.commit()

    async with sessions() as session:
        assessment = await assess_activation_readiness(session, "candidate")
        assert assessment["ready"] is True
        assert assessment["ratios"] == {"archive_coverage": 1.0, "body_coverage": 1.0}

        session.add_all(
            [
                TDocRow(
                    id="R2-2600002",
                    dataset_version_id="candidate",
                    meeting_id="RAN2-134",
                    title="Missing actionable body",
                    status="agreed",
                ),
                TDocRow(
                    id="R2-2600003",
                    dataset_version_id="candidate",
                    meeting_id="RAN2-134",
                    title="Reserved document",
                    status="reserved",
                ),
                ArtifactVersionRow(
                    id="reserved-artifact",
                    dataset_version_id="candidate",
                    meeting_id="RAN2-134",
                    kind="tdoc",
                    source_url="https://www.3gpp.org/R2-2600003.zip",
                    filename="R2-2600003.zip",
                    sha256="c" * 64,
                    content_type="application/zip",
                    object_key="sha256/cc/reserved",
                    parse_status="parsed",
                ),
            ]
        )
        await session.commit()
        assessment = await assess_activation_readiness(session, "candidate")
        assert assessment["ready"] is False
        assert assessment["ratios"] == {"archive_coverage": 0.5, "body_coverage": 0.5}
        assert assessment["missing"] == {
            "archive_tdoc_ids": ["R2-2600002"],
            "body_tdoc_ids": ["R2-2600002"],
        }
    await engine.dispose()


@pytest.mark.asyncio
async def test_dataset_activation_is_atomic(tmp_path) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                DatasetVersionRow(
                    id="old",
                    state=DatasetState.ACTIVE,
                    created_at=datetime.now(UTC),
                    is_active=True,
                    stats={},
                ),
                DatasetVersionRow(
                    id="new",
                    state=DatasetState.VALIDATED,
                    created_at=datetime.now(UTC),
                    is_active=False,
                    stats={},
                ),
            ]
        )
        await session.commit()
    async with sessions() as session:
        await activate_dataset(session, "new")
        await session.commit()
    async with sessions() as session:
        rows = list((await session.scalars(select(DatasetVersionRow))).all())
        active = [row.id for row in rows if row.is_active]
        assert active == ["new"]
        assert next(row for row in rows if row.id == "old").state == DatasetState.SUPERSEDED
    await engine.dispose()


@pytest.mark.asyncio
async def test_dataset_activation_rejects_missing_and_unvalidated_versions(tmp_path) -> None:
    engine, sessions = create_engine_and_session(
        DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path}/guards.db")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            DatasetVersionRow(
                id="candidate",
                state=DatasetState.BUILDING,
                created_at=datetime.now(UTC),
                is_active=False,
                stats={},
            )
        )
        await session.commit()
    async with sessions() as session:
        with pytest.raises(ValueError, match="does not exist"):
            await activate_dataset(session, "missing")
        with pytest.raises(ValueError, match="only validated"):
            await activate_dataset(session, "candidate")
    await engine.dispose()
