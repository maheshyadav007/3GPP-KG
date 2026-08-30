from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from threegpp_kg.fixtures import demo_repository
from threegpp_kg.mcp_server import create_mcp_server
from threegpp_kg.service import KnowledgeService

CALLS: dict[str, dict[str, Any]] = {
    "search_tdocs": {"query": "simultaneous", "meeting_ids": ["RAN2-133"]},
    "get_relevant_passages": {
        "query": "What was agreed?",
        "tdoc_ids": ["R2-2601389"],
    },
    "get_meeting_brief": {"meeting_id": "RAN2-133"},
}


async def generate() -> dict[str, Any]:
    server = create_mcp_server(KnowledgeService(demo_repository()))
    transcripts: list[dict[str, Any]] = []
    for tool, arguments in CALLS.items():
        _, structured = await server.call_tool(tool, arguments)
        transcripts.append({"tool": tool, "arguments": arguments, "response": structured})
    return {
        "scope": "deterministic development fixture",
        "transcripts": transcripts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(generate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
