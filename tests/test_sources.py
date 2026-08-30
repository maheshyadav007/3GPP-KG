from pathlib import Path

from threegpp_kg.config import WorkingGroupConfig, load_working_groups
from threegpp_kg.constants import ArtifactKind
from threegpp_kg.sources import SourceAdapter


def test_ran2_meeting_discovery_rejects_untrusted_hosts() -> None:
    html = Path("tests/fixtures/ran2_root.html").read_text()
    adapter = SourceAdapter(load_working_groups()["RAN2"], {"www.3gpp.org"})
    meetings = adapter.discover_meetings(html)
    assert [meeting.id for meeting in meetings] == ["RAN2-132", "RAN2-133", "RAN2-133-bis"]
    assert all("evil.example" not in meeting.url for meeting in meetings)


def test_ran2_artifact_classification() -> None:
    html = Path("tests/fixtures/ran2_docs.html").read_text()
    adapter = SourceAdapter(load_working_groups()["RAN2"], {"www.3gpp.org"})
    artifacts = adapter.discover_artifacts(
        html,
        "https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_133/Docs/",
        "RAN2-133",
        "documents",
    )
    assert [(item.filename, item.kind) for item in artifacts] == [
        ("R2-2601389.zip", ArtifactKind.TDOC),
        ("TDoc_List_Meeting_RAN2#133.xlsx", ArtifactKind.TDOC_LIST),
    ]


def test_sa2_location_suffix_is_not_a_meeting_variant() -> None:
    config = WorkingGroupConfig(
        id="SA2",
        name="SA2",
        tsg="SA",
        root_url="https://www.3gpp.org/ftp/tsg_sa/WG2_Arch/",
        meeting_pattern=(
            r"^TSGS2_(?P<number>\d+)(?P<variant>-?AHE|-?AH-e|bis(?:-e)?|[-_]e)?(?:_.*)?$"
        ),
        tdoc_prefix="S2",
        directories={},
        artifact_patterns={},
    )
    adapter = SourceAdapter(config, {"www.3gpp.org"})
    meetings = adapter.discover_meetings(
        """
        <a href="TSGS2_172_Dallas_2025-11/">regular</a>
        <a href="TSGS2_166AH-e_Electronic_2025-01/">ad hoc</a>
        """
    )
    assert [(item.id, item.variant) for item in meetings] == [
        ("SA2-166-ah-e", "ah-e"),
        ("SA2-172", ""),
    ]
