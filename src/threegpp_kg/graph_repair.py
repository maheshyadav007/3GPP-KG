from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .ingestion.pipeline import persist_tdoc_graph_batch, validate_dataset
from .repository import SqlRepository
from .storage.database import (
    DatasetVersionRow,
    KnowledgeEdgeRow,
    KnowledgeNodeRow,
    MeetingRow,
    TDocRow,
)


async def rebuild_graph(session: AsyncSession, dataset_version_id: str) -> dict[str, Any]:
    dataset = await session.get(DatasetVersionRow, dataset_version_id)
    if dataset is None:
        raise ValueError(f"dataset {dataset_version_id} does not exist")
    if dataset.is_active:
        raise ValueError("graph rebuilding is restricted to inactive datasets")
    previous_stats = dict(dataset.stats or {})
    await session.execute(
        delete(KnowledgeEdgeRow).where(
            KnowledgeEdgeRow.dataset_version_id == dataset_version_id
        )
    )
    await session.execute(
        delete(KnowledgeNodeRow).where(
            KnowledgeNodeRow.dataset_version_id == dataset_version_id
        )
    )
    meeting_rows = list(
        (
            await session.scalars(
                select(MeetingRow)
                .where(MeetingRow.dataset_version_id == dataset_version_id)
                .order_by(MeetingRow.id)
            )
        ).all()
    )
    for meeting_row in meeting_rows:
        tdoc_rows = list(
            (
                await session.scalars(
                    select(TDocRow).where(
                        TDocRow.dataset_version_id == dataset_version_id,
                        TDocRow.meeting_id == meeting_row.id,
                    )
                )
            ).all()
        )
        await persist_tdoc_graph_batch(
            session,
            dataset_version_id,
            SqlRepository._meeting(meeting_row),
            [SqlRepository._tdoc(row) for row in tdoc_rows],
        )
    stats = await validate_dataset(session, dataset_version_id)
    dataset.stats = {
        **previous_stats,
        **stats,
        "graph_rebuild": {
            "canonical_membership": True,
            "meetings": len(meeting_rows),
            "nodes": stats["nodes"],
            "edges": stats["edges"],
        },
    }
    await session.flush()
    return {"dataset_version": dataset_version_id, **stats, "validated": True}
