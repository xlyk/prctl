"""Tests for ``queue`` subcommand and its pure categorizer."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from prctl.cli import app, categorize_pr
from tests.conftest import FakeProc

# ---------- pure categorizer ----------


BASE_PR = {
    "number": 1,
    "title": "t",
    "author": {"login": "other"},
    "isDraft": False,
    "reviewDecision": "",
    "mergeable": "MERGEABLE",
    "headRefName": "h",
    "statusCheckRollup": [],
}


@pytest.mark.parametrize(
    "rd,mergeable,checks,expected",
    [
        ("APPROVED", "MERGEABLE", ["SUCCESS"], "ready_merge"),
        ("APPROVED", "MERGEABLE", ["SUCCESS", "SKIPPED"], "ready_merge"),
        ("APPROVED", "MERGEABLE", ["FAILURE"], None),  # approved but CI failing → not ready
        ("APPROVED", "CONFLICTING", ["SUCCESS"], None),  # approved but conflict → not ready
        ("", "MERGEABLE", ["SUCCESS"], None),  # no approval → not ready
    ],
)
def test_categorize_ready_merge(rd: str, mergeable: str, checks: list[str], expected: str | None) -> None:
    pr = {**BASE_PR, "reviewDecision": rd, "mergeable": mergeable, "statusCheckRollup_conclusions": checks}
    got = categorize_pr(pr, last_commit=None, last_caller_feedback=None)
    if expected == "ready_merge":
        assert got == "ready_merge"
    else:
        assert got != "ready_merge"


def test_categorize_initial_review_when_caller_never_engaged() -> None:
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    assert categorize_pr(pr, last_commit="2026-04-23T10:00:00Z", last_caller_feedback=None) == "initial_review"


def test_categorize_another_round_when_new_commit_after_feedback() -> None:
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    got = categorize_pr(
        pr,
        last_commit="2026-04-23T12:00:00Z",
        last_caller_feedback="2026-04-23T10:00:00Z",
    )
    assert got == "another_round"


def test_categorize_awaiting_author_when_feedback_newer_than_commit() -> None:
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    got = categorize_pr(
        pr,
        last_commit="2026-04-23T10:00:00Z",
        last_caller_feedback="2026-04-23T12:00:00Z",
    )
    assert got == "awaiting_author"


# ---------- queue orchestration ----------


def test_queue_end_to_end(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["gh", "api", "user"], stdout=json.dumps({"login": "caller"}))

    # repo 1: one PR by someone else, ready for initial review
    fake_proc.register(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "o/r1",
        ],
        stdout=json.dumps(
            [
                {
                    "number": 10,
                    "title": "feature",
                    "author": {"login": "other"},
                    "isDraft": False,
                    "reviewDecision": "",
                    "mergeable": "MERGEABLE",
                    "headRefName": "h",
                    "statusCheckRollup": [{"__typename": "CheckRun", "conclusion": "SUCCESS"}],
                },
                {
                    "number": 11,
                    "title": "mine",
                    "author": {"login": "caller"},
                    "isDraft": False,
                    "reviewDecision": "",
                    "mergeable": "MERGEABLE",
                    "headRefName": "h",
                    "statusCheckRollup": [],
                },
            ]
        ),
    )
    # commits + reviews + comments for PR #10
    fake_proc.register(
        ["gh", "api", "repos/o/r1/pulls/10/commits"],
        stdout=json.dumps([{"commit": {"committer": {"date": "2026-04-23T10:00:00Z"}}}]),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/o/r1/pulls/10/reviews"],
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/o/r1/pulls/10/comments"],
        stdout=json.dumps([]),
    )

    result = cli.invoke(app, ["queue", "--repos", "o/r1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)

    assert data["initial_review"][0]["number"] == 10
    assert data["ready_merge"] == []
    assert data["another_round"] == []
    assert data["awaiting_author"] == []
    # caller's own PR is excluded everywhere
    all_prs = (
        data["ready_merge"]
        + data["initial_review"]
        + data["another_round"]
        + data["awaiting_author"]
        + data.get("drafts", [])
    )
    assert all(p["number"] != 11 for p in all_prs)
