from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from threegpp_kg.config import load_settings


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_version(*command: str) -> str | None:
    if not shutil.which(command[0]):
        return None
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if output else None


def find_tool(name: str, *fallbacks: str) -> str | None:
    installed = shutil.which(name)
    if installed:
        return installed
    return next((path for path in fallbacks if Path(path).is_file()), None)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a redacted verification proof manifest")
    parser.add_argument("run_id")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts" / "test-runs" / args.run_id
    output.mkdir(parents=True, exist_ok=True)

    psql = find_tool("psql", "/opt/homebrew/opt/postgresql@17/bin/psql")
    docker = find_tool("docker")
    environment = {
        "captured_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "tools": {
            "uv": command_version("uv", "--version"),
            "node": command_version("node", "--version"),
            "npm": command_version("npm", "--version"),
            "docker": command_version(docker, "--version") if docker else None,
            "psql": command_version(psql, "--version") if psql else None,
        },
        "unavailable_production_services": [
            name for name, path in (("docker", docker), ("psql", psql)) if path is None
        ],
    }
    write_json(output / "environment.json", environment)
    write_json(output / "effective-config.redacted.json", load_settings().redacted())

    git_commit = command_version("git", "rev-parse", "HEAD") if (root / ".git").exists() else None
    manifest = {
        "run_id": args.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": git_commit,
        "git_note": None if git_commit else "Workspace is not a Git repository.",
        "dependency_locks": {
            "uv.lock": sha256(root / "uv.lock"),
            "web/package-lock.json": sha256(root / "web" / "package-lock.json"),
        },
        "proof_files": sorted(
            path.name for path in output.iterdir() if path.name != "manifest.json"
        ),
        "scope": "Local deterministic verification; production infrastructure gates are separate.",
    }
    write_json(output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
