from __future__ import annotations

import json
import sys

import pytest

from threegpp_kg import cli


@pytest.mark.parametrize("command", ["check-config", "list-working-groups"])
def test_read_only_cli_commands_emit_json(monkeypatch, capsys, command: str) -> None:
    monkeypatch.setattr(sys, "argv", ["threegpp-kg", command])
    cli.main()
    assert json.loads(capsys.readouterr().out)


def test_validate_sources_cli_writes_result_and_exits_on_failure(monkeypatch, tmp_path) -> None:
    async def fake_validation():
        return {"passed": False, "working_groups": {}}

    output = tmp_path / "result.json"
    monkeypatch.setattr(cli, "validate_configured_sources", fake_validation)
    monkeypatch.setattr(sys, "argv", ["threegpp-kg", "validate-sources", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert json.loads(output.read_text())["passed"] is False
