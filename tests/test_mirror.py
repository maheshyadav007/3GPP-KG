from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from threegpp_kg import mirror
from threegpp_kg.config import load_settings, load_working_groups
from threegpp_kg.constants import ArtifactKind
from threegpp_kg.mirror import mirror_working_group
from threegpp_kg.sources.adapter import DiscoveredArtifact


@pytest.mark.asyncio
async def test_local_corpus_mirror_checkpoints_and_resumes_without_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings().model_copy(
        update={
            "http": load_settings().http.model_copy(
                update={"requests_per_second": 1000, "max_concurrency": 2}
            ),
            "object_store": load_settings().object_store.model_copy(
                update={"local_path": tmp_path / "objects"}
            ),
        }
    )
    group = load_working_groups()["RAN2"]
    artifacts = [
        DiscoveredArtifact(
            ArtifactKind.TDOC,
            f"https://www.3gpp.org/file-{index}.bin",
            f"R2-260000{index}.bin",
            "RAN2-135",
        )
        for index in range(2)
    ]

    async def discover(*args: object) -> dict[str, list[DiscoveredArtifact]]:
        return {"documents": artifacts}

    monkeypatch.setattr(mirror, "_discover_meeting_artifacts", discover)
    manifest = tmp_path / "manifest.json"
    root_html = '<a href="TSGR2_135/">TSGR2_135/</a>'
    with respx.mock:
        respx.get(group.root_url).mock(return_value=httpx.Response(200, text=root_html))
        routes = [
            respx.get(item.url).mock(return_value=httpx.Response(200, content=b"content"))
            for item in artifacts
        ]
        first = await mirror_working_group(settings, group, ["RAN2-135"], manifest)
        second = await mirror_working_group(settings, group, ["RAN2-135"], manifest)

    assert first["downloaded"] == 2
    assert second["downloaded"] == 0
    assert second["skipped_existing"] == 2
    assert all(route.call_count == 1 for route in routes)
    stored = json.loads(manifest.read_text())
    assert len(stored["artifacts"]) == 2
    assert {entry["status"] for entry in stored["artifacts"].values()} == {"downloaded"}
