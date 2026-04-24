"""Tests for the ``ci-wait`` subcommand."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import prctl.cli as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def test_ci_wait_green_on_first_poll(fake_proc: FakeProc, cli: CliRunner, no_sleep: None) -> None:
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "42", "--json", "statusCheckRollup"],
        stdout=json.dumps({"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]}),
    )

    result = cli.invoke(app, ["ci-wait", "42", "--repo", "o/r"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["state"] == "green"
    assert data["checks"]["fail"] == []
    assert data["checks"]["pending"] == []


def test_ci_wait_exits_1_on_failing_check(fake_proc: FakeProc, cli: CliRunner, no_sleep: None) -> None:
    fake_proc.register_seq(
        ["gh", "pr", "view", "--repo", "o/r", "42", "--json", "statusCheckRollup"],
        [
            {"stdout": json.dumps({"statusCheckRollup": [{"status": "IN_PROGRESS"}]})},
            {"stdout": json.dumps({"statusCheckRollup": [{"conclusion": "FAILURE"}]})},
        ],
    )

    result = cli.invoke(app, ["ci-wait", "42", "--repo", "o/r", "--interval", "0"])
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert data["state"] == "failing"
    assert data["checks"]["fail"] == ["FAILURE"]


def test_ci_wait_exits_2_on_timeout(
    fake_proc: FakeProc, cli: CliRunner, monkeypatch: pytest.MonkeyPatch, no_sleep: None
) -> None:
    # simulate clock so monotonic advances past timeout after two polls
    clock = iter([0.0, 0.1, 999.0, 999.1])
    import prctl.cli as mod

    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "42", "--json", "statusCheckRollup"],
        stdout=json.dumps({"statusCheckRollup": [{"status": "IN_PROGRESS"}, {"conclusion": "SUCCESS"}]}),
    )

    result = cli.invoke(app, ["ci-wait", "42", "--repo", "o/r", "--timeout", "1", "--interval", "0"])
    assert result.exit_code == 2, result.output
    data = json.loads(result.stdout)
    assert data["state"] == "timeout"
    assert "IN_PROGRESS" in data["checks"]["pending"] or "PENDING" in data["checks"]["pending"]
