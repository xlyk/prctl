"""Tests for the ``stack`` subcommand."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc


def test_stack_walks_base_chain_bottom_to_top(fake_proc: FakeProc, cli: CliRunner) -> None:
    """Given a top-of-stack PR number, walk baseRefName back to the main-rooted PR."""
    # seed PR 3 → base=stack/mid (PR 2) → base=stack/bottom (PR 1) → base=main
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "3", "--json"],
        stdout=json.dumps({"number": 3, "headRefName": "stack/top", "baseRefName": "stack/mid"}),
    )
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "open", "--head", "stack/mid"],
        stdout=json.dumps([{"number": 2, "headRefName": "stack/mid", "baseRefName": "stack/bottom"}]),
    )
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "open", "--head", "stack/bottom"],
        stdout=json.dumps([{"number": 1, "headRefName": "stack/bottom", "baseRefName": "main"}]),
    )
    fake_proc.register(["git", "worktree", "list", "--porcelain"], stdout="")

    result = cli.invoke(app, ["stack", "--repo", "o/r", "--seed", "3"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert [p["number"] for p in data] == [1, 2, 3]
    assert data[0]["base"] == "main"


def test_stack_detects_worktree_path(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "5", "--json"],
        stdout=json.dumps({"number": 5, "headRefName": "feat/x", "baseRefName": "main"}),
    )
    fake_proc.register(
        ["git", "worktree", "list", "--porcelain"],
        stdout=(
            "worktree /primary\n"
            "HEAD deadbeef\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /tmp/agent\n"
            "HEAD cafebabe\n"
            "branch refs/heads/feat/x\n"
        ),
    )

    result = cli.invoke(app, ["stack", "--repo", "o/r", "--seed", "5"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["worktree"] == "/tmp/agent"
