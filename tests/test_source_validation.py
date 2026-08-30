from __future__ import annotations

import json

import httpx
import pytest
import respx

from threegpp_kg.config import WorkingGroupConfig, load_settings
from threegpp_kg.source_validation import validate_configured_sources, write_validation_result


def validation_group() -> WorkingGroupConfig:
    return WorkingGroupConfig(
        id="TEST",
        name="Test WG",
        tsg="TEST",
        root_url="https://www.3gpp.org/root/",
        meeting_pattern=r"^MEET_(?P<number>\d+)$",
        tdoc_prefix="T",
        directories={"documents": ["Docs"], "reports": ["Report"], "agenda": ["Missing"]},
        artifact_patterns={
            "tdoc_list": r"(?i)TDoc_List_.*\.xlsx$",
            "tdoc": r"(?i)T-[0-9]+\.zip$",
            "report": r"(?i)T-[0-9]+\.zip$",
        },
        validation_meetings=["MEET_1", "MEET_0"],
    )


@pytest.mark.asyncio
async def test_source_validation_records_roles_missing_samples_and_output(tmp_path) -> None:
    root = '<a href="MEET_1/">MEET_1/</a>'
    meeting = '<a href="Docs/">Docs/</a><a href="Report/">Report/</a>'
    with respx.mock:
        respx.get("https://www.3gpp.org/root/").mock(return_value=httpx.Response(200, text=root))
        respx.get("https://www.3gpp.org/root/MEET_1/").mock(
            return_value=httpx.Response(200, text=meeting)
        )
        respx.get("https://www.3gpp.org/root/MEET_1/Docs/").mock(
            return_value=httpx.Response(200, text='<a href="TDoc_List_1.xlsx">TDoc_List_1.xlsx</a>')
        )
        respx.get("https://www.3gpp.org/root/MEET_1/Report/").mock(
            return_value=httpx.Response(200, text='<a href="T-1.zip">T-1.zip</a>')
        )
        result = await validate_configured_sources(load_settings(), {"TEST": validation_group()})

    sample, missing = result["working_groups"]["TEST"]["samples"]
    assert sample["roles"]["documents"]["artifact_count"] == 1
    assert sample["roles"]["reports"]["kinds"] == ["report"]
    assert sample["roles"]["agenda"]["present"] is False
    assert missing == {"source_name": "MEET_0", "passed": False, "error": "not found"}
    assert result["passed"] is False

    output = tmp_path / "proof" / "sources.json"
    write_validation_result(result, output)
    assert json.loads(output.read_text())["source"] == "official 3GPP directory listings"
