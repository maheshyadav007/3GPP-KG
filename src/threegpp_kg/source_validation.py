from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import Settings, WorkingGroupConfig, load_settings, load_working_groups
from .sources.adapter import SourceAdapter


async def validate_configured_sources(
    settings: Settings | None = None,
    groups: dict[str, WorkingGroupConfig] | None = None,
) -> dict[str, Any]:
    settings = settings or load_settings()
    groups = groups or load_working_groups()
    timeout = httpx.Timeout(settings.http.timeout_seconds)
    headers = {"User-Agent": settings.http.user_agent}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        results = await asyncio.gather(
            *(
                _validate_group(group, settings.security.allowed_source_hosts, client)
                for group in groups.values()
            )
        )
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "official 3GPP directory listings",
        "working_groups": {result["working_group"]: result for result in results},
        "passed": all(result["passed"] for result in results),
    }


async def _validate_group(
    group: WorkingGroupConfig,
    allowed_hosts: list[str],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    adapter = SourceAdapter(group, set(allowed_hosts))
    root_response = await client.get(group.root_url)
    root_response.raise_for_status()
    discovered = adapter.discover_meetings(root_response.text)
    by_source_name = {meeting.source_name: meeting for meeting in discovered}
    samples: list[dict[str, Any]] = []
    for source_name in group.validation_meetings:
        meeting = by_source_name.get(source_name)
        if meeting is None:
            samples.append({"source_name": source_name, "passed": False, "error": "not found"})
            continue
        response = await client.get(meeting.url)
        response.raise_for_status()
        directory_names = _directory_names(response.text)
        roles: dict[str, Any] = {}
        for role, candidates in group.directories.items():
            selected = next(
                (name for name in candidates if name == "." or name in directory_names), None
            )
            if selected is None:
                roles[role] = {"present": False, "artifact_count": 0}
                continue
            listing_url = meeting.url if selected == "." else urljoin(meeting.url, selected + "/")
            listing_html = response.text
            if selected != ".":
                listing_response = await client.get(listing_url)
                listing_response.raise_for_status()
                listing_html = listing_response.text
            artifacts = adapter.discover_artifacts(listing_html, listing_url, meeting.id, role)
            roles[role] = {
                "present": True,
                "directory": selected,
                "artifact_count": len(artifacts),
                "kinds": sorted({artifact.kind.value for artifact in artifacts}),
            }
        samples.append(
            {
                "source_name": source_name,
                "meeting_id": meeting.id,
                "url": meeting.url,
                "roles": roles,
                "passed": any(value["artifact_count"] > 0 for value in roles.values()),
            }
        )
    return {
        "working_group": group.id,
        "root_url": group.root_url,
        "discovered_meetings": len(discovered),
        "samples": samples,
        "passed": bool(samples) and all(sample["passed"] for sample in samples),
    }


def _directory_names(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        anchor.get_text(strip=True).rstrip("/")
        for anchor in soup.find_all("a", href=True)
        if anchor.get_text(strip=True)
    }


def write_validation_result(result: dict[str, Any], output: Path) -> None:
    import json

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
