"""Tests for ``queue`` subcommand and its pure categorizer."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from prctl.cli import _unresolved_caller_threads, app, categorize_pr
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


def test_categorize_another_round_demoted_when_no_unresolved_caller_threads() -> None:
    """New commits landed, but every caller thread is resolved/outdated → not actionable."""
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    got = categorize_pr(
        pr,
        last_commit="2026-04-23T12:00:00Z",
        last_caller_feedback="2026-04-23T10:00:00Z",
        unresolved_caller_threads=0,
    )
    assert got == "awaiting_author"


def test_categorize_another_round_retained_when_unresolved_threads_exist() -> None:
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    got = categorize_pr(
        pr,
        last_commit="2026-04-23T12:00:00Z",
        last_caller_feedback="2026-04-23T10:00:00Z",
        unresolved_caller_threads=2,
    )
    assert got == "another_round"


def test_categorize_initial_review_unaffected_by_unresolved_threads() -> None:
    """Caller never engaged → initial_review regardless of the (trivially-zero) thread count."""
    pr = {**BASE_PR, "statusCheckRollup_conclusions": ["SUCCESS"]}
    got = categorize_pr(
        pr,
        last_commit="2026-04-23T10:00:00Z",
        last_caller_feedback=None,
        unresolved_caller_threads=0,
    )
    assert got == "initial_review"


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
    # graphql reviewThreads — always fetched for non-draft PRs
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}),
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


# ---------- _unresolved_caller_threads helper ----------


def _graphql_stdout(threads: list[dict[str, object]]) -> str:
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}})


def _thread(*, resolved: bool = False, outdated: bool = False, authors: list[str] | None = None) -> dict[str, object]:
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [{"author": {"login": a}} for a in (authors or [])]},
    }


def test_unresolved_caller_threads_counts_open_threads_where_caller_commented(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=_graphql_stdout(
            [
                _thread(authors=["caller", "other"]),  # unresolved + caller commented → count
                _thread(authors=["caller"]),  # unresolved + caller commented → count
            ]
        ),
    )
    assert _unresolved_caller_threads("o", "r", 1, "caller") == 2


def test_unresolved_caller_threads_skips_resolved(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=_graphql_stdout(
            [
                _thread(resolved=True, authors=["caller"]),
                _thread(authors=["caller"]),
            ]
        ),
    )
    assert _unresolved_caller_threads("o", "r", 1, "caller") == 1


def test_unresolved_caller_threads_skips_outdated(fake_proc: FakeProc) -> None:
    """Outdated threads are treated as addressed — the code they pointed at moved."""
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=_graphql_stdout(
            [
                _thread(outdated=True, authors=["caller"]),
                _thread(authors=["caller"]),
            ]
        ),
    )
    assert _unresolved_caller_threads("o", "r", 1, "caller") == 1


def test_unresolved_caller_threads_skips_threads_caller_didnt_comment_on(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=_graphql_stdout(
            [
                _thread(authors=["other"]),
                _thread(authors=["other", "someone_else"]),
            ]
        ),
    )
    assert _unresolved_caller_threads("o", "r", 1, "caller") == 0


# ---------- queue orchestration with thread-awareness ----------


def _queue_pr_fixture(
    fake_proc: FakeProc,
    *,
    repo: str,
    number: int,
    author: str = "other",
    last_commit: str,
    caller_review_at: str | None,
    thread_nodes: list[dict[str, object]],
) -> None:
    fake_proc.register(
        ["gh", "api", f"repos/{repo}/pulls/{number}/commits"],
        stdout=json.dumps([{"commit": {"committer": {"date": last_commit}}}]),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/reviews"],
        stdout=json.dumps(
            [{"user": {"login": "caller"}, "submitted_at": caller_review_at}] if caller_review_at else []
        ),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/comments"],
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=_graphql_stdout(thread_nodes),
    )


def test_queue_demotes_another_round_when_all_caller_threads_resolved(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["gh", "api", "user"], stdout=json.dumps({"login": "caller"}))
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r1"],
        stdout=json.dumps(
            [
                {
                    "number": 42,
                    "title": "feat",
                    "author": {"login": "other"},
                    "isDraft": False,
                    "reviewDecision": "",
                    "mergeable": "MERGEABLE",
                    "headRefName": "h",
                    "statusCheckRollup": [{"__typename": "CheckRun", "conclusion": "SUCCESS"}],
                }
            ]
        ),
    )
    _queue_pr_fixture(
        fake_proc,
        repo="o/r1",
        number=42,
        last_commit="2026-04-24T12:00:00Z",  # newer than caller_review_at
        caller_review_at="2026-04-23T10:00:00Z",
        thread_nodes=[_thread(resolved=True, authors=["caller"])],
    )

    result = cli.invoke(app, ["queue", "--repos", "o/r1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)

    assert data["another_round"] == []
    assert data["awaiting_author"][0]["number"] == 42
    assert data["awaiting_author"][0]["unresolved_caller_threads"] == 0


def test_queue_keeps_another_round_when_unresolved_threads_exist(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["gh", "api", "user"], stdout=json.dumps({"login": "caller"}))
    fake_proc.register(
        ["gh", "pr", "list", "--repo", "o/r1"],
        stdout=json.dumps(
            [
                {
                    "number": 43,
                    "title": "feat",
                    "author": {"login": "other"},
                    "isDraft": False,
                    "reviewDecision": "",
                    "mergeable": "MERGEABLE",
                    "headRefName": "h",
                    "statusCheckRollup": [{"__typename": "CheckRun", "conclusion": "SUCCESS"}],
                }
            ]
        ),
    )
    _queue_pr_fixture(
        fake_proc,
        repo="o/r1",
        number=43,
        last_commit="2026-04-24T12:00:00Z",
        caller_review_at="2026-04-23T10:00:00Z",
        thread_nodes=[
            _thread(authors=["caller"]),  # unresolved
            _thread(resolved=True, authors=["caller"]),  # resolved
        ],
    )

    result = cli.invoke(app, ["queue", "--repos", "o/r1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)

    assert data["awaiting_author"] == []
    assert data["another_round"][0]["number"] == 43
    assert data["another_round"][0]["unresolved_caller_threads"] == 1
