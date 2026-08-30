from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from threegpp_kg.api import create_app
from threegpp_kg.fixtures import demo_repository
from threegpp_kg.service import KnowledgeService


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def run_probe(users: int) -> dict[str, Any]:
    app = create_app(KnowledgeService(demo_repository()))
    start = asyncio.Event()
    latencies: dict[str, list[float]] = defaultdict(list)
    errors: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://load-probe",
        timeout=10,
    ) as client:

        async def user(index: int) -> None:
            await start.wait()
            for route in ("/health", "/api/graph?query=carrier&limit=100"):
                began = time.perf_counter()
                response = await client.get(route)
                latencies[route].append((time.perf_counter() - began) * 1000)
                if response.status_code != 200:
                    errors.append({"user": index, "route": route, "status": response.status_code})

        tasks = [asyncio.create_task(user(index)) for index in range(users)]
        began = time.perf_counter()
        start.set()
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - began

    routes = {
        route: {
            "requests": len(values),
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(percentile(values, 0.95), 3),
            "max_ms": round(max(values), 3),
        }
        for route, values in latencies.items()
    }
    return {
        "scope": "in-process ASGI fixture probe; excludes network, PostgreSQL, pgvector, and S3",
        "concurrent_users": users,
        "requests": sum(len(values) for values in latencies.values()),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_rps": round(sum(len(values) for values in latencies.values()) / elapsed, 2),
        "error_count": len(errors),
        "errors": errors[:20],
        "routes": routes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.users))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
