from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import WorkingGroupConfig
from ..constants import ArtifactKind


@dataclass(frozen=True, slots=True)
class DiscoveredMeeting:
    id: str
    working_group_id: str
    number: int
    variant: str
    url: str
    source_name: str


@dataclass(frozen=True, slots=True)
class DiscoveredArtifact:
    kind: ArtifactKind
    url: str
    filename: str
    meeting_id: str


class SourceAdapter:
    """Maps WG-specific directory listings into the canonical discovery model."""

    def __init__(self, config: WorkingGroupConfig, allowed_hosts: set[str]) -> None:
        self.config = config
        self.allowed_hosts = allowed_hosts
        self._meeting_re = re.compile(config.meeting_pattern)
        self._artifact_patterns = {
            key: re.compile(pattern) for key, pattern in config.artifact_patterns.items()
        }

    def discover_meetings(self, html: str) -> list[DiscoveredMeeting]:
        meetings: dict[str, DiscoveredMeeting] = {}
        for url, name in self._links(html, self.config.root_url):
            match = self._meeting_re.fullmatch(name.rstrip("/"))
            if not match:
                continue
            number = int(match.group("number"))
            variant = _normalize_variant(match.groupdict().get("variant") or "")
            meeting_id = f"{self.config.id}-{number}{('-' + variant) if variant else ''}"
            meetings[meeting_id] = DiscoveredMeeting(
                id=meeting_id,
                working_group_id=self.config.id,
                number=number,
                variant=variant,
                url=url.rstrip("/") + "/",
                source_name=name.rstrip("/"),
            )
        return sorted(meetings.values(), key=lambda item: (item.number, item.variant))

    def discover_artifacts(
        self,
        html: str,
        listing_url: str,
        meeting_id: str,
        directory_role: str,
    ) -> list[DiscoveredArtifact]:
        artifacts: dict[str, DiscoveredArtifact] = {}
        for url, name in self._links(html, listing_url):
            filename = unquote(name.rstrip("/"))
            if filename in {"", ".", ".."} or name.endswith("/"):
                continue
            kind = self._classify(filename, directory_role)
            artifacts[url] = DiscoveredArtifact(kind, url, filename, meeting_id)
        return sorted(artifacts.values(), key=lambda item: item.filename.lower())

    def _classify(self, filename: str, directory_role: str) -> ArtifactKind:
        ordered = (
            ("agenda_csv", ArtifactKind.AGENDA),
            ("tdoc_list", ArtifactKind.TDOC_LIST),
            ("tdoc", ArtifactKind.TDOC),
            ("report", ArtifactKind.REPORT),
        )
        for key, kind in ordered:
            pattern = self._artifact_patterns.get(key)
            if pattern and pattern.search(filename):
                if kind == ArtifactKind.TDOC and directory_role == "reports":
                    return ArtifactKind.REPORT
                return kind
        if directory_role == "reports":
            return ArtifactKind.REPORT
        return ArtifactKind.OTHER

    def _links(self, html: str, base_url: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[tuple[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if href.startswith(("?", "#", "javascript:")):
                continue
            url = urljoin(base_url, href)
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
                continue
            name = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
            if not name:
                continue
            links.append((url, name + ("/" if parsed.path.endswith("/") else "")))
        return links


def _normalize_variant(value: str) -> str:
    normalized = value.strip("_- ").lower()
    if normalized in {"ahe", "ah-e"}:
        return "ah-e"
    return normalized
