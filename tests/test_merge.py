"""Tests for ``safe-merge`` and ``rebase-onto-main`` subcommands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc


def test_safe_merge_deletes_branch_when_no_children(fake_proc: FakeProc, cli: CliRunner) -> None:
    # step 1: look up the PR's head ref
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "10", "--json"],
        stdout=json.dumps({"number": 10, "headRefName": "feat/x"}),
    )
    # step 2: children check — none
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "open", "--base", "feat/x"],
        stdout=json.dumps([]),
    )
    # step 3: merge with --delete-branch
    fake_proc.register(
        ["gh", "pr", "merge", "--repo", "o/r", "10", "--squash", "--delete-branch"],
        stdout="",
    )
    # step 4: fetch merge commit sha
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "10", "--json", "mergeCommit"],
        stdout=json.dumps({"mergeCommit": {"oid": "abc123"}}),
    )

    result = cli.invoke(app, ["safe-merge", "--repo", "o/r", "10"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "merge_sha": "abc123",
        "deleted_branch": True,
        "children_blocked": [],
    }


def test_safe_merge_preserves_branch_when_children_exist(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "10", "--json"],
        stdout=json.dumps({"number": 10, "headRefName": "stack/bottom"}),
    )
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "open", "--base", "stack/bottom"],
        stdout=json.dumps([{"number": 11}, {"number": 12}]),
    )
    fake_proc.register(
        ["gh", "pr", "merge", "--repo", "o/r", "10", "--squash"],
        stdout="",
    )
    fake_proc.register(
        ["gh", "pr", "view", "--repo", "o/r", "10", "--json", "mergeCommit"],
        stdout=json.dumps({"mergeCommit": {"oid": "def456"}}),
    )

    result = cli.invoke(app, ["safe-merge", "--repo", "o/r", "10"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["merge_sha"] == "def456"
    assert data["deleted_branch"] is False
    assert sorted(data["children_blocked"]) == [11, 12]

    # confirm --delete-branch is NOT in the merge call
    merge_calls = [c for c in fake_proc.calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls
    assert "--delete-branch" not in merge_calls[0]


def test_rebase_onto_main_plain(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["git", "fetch", "origin", "main"], stdout="")
    fake_proc.register(["git", "rebase", "origin/main"], stdout="")
    fake_proc.register(["git", "rev-parse", "HEAD"], stdout="newhead\n")

    result = cli.invoke(app, ["rebase-onto-main"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "strategy": "plain",
        "old_base": None,
        "head_after": "newhead",
    }


def test_rebase_onto_main_with_old_base(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["git", "fetch", "origin", "main"], stdout="")
    fake_proc.register(
        ["git", "rebase", "--onto", "origin/main", "deadbeef"],
        stdout="",
    )
    fake_proc.register(["git", "rev-parse", "HEAD"], stdout="newhead\n")

    result = cli.invoke(app, ["rebase-onto-main", "--old-base", "deadbeef"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "strategy": "onto",
        "old_base": "deadbeef",
        "head_after": "newhead",
    }
