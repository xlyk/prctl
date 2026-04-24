"""Tests for the ``comments`` subcommand."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc


def _register_graphql_empty_threads(fp: FakeProc) -> None:
    """Default the GraphQL reviewThreads query to an empty node list."""
    fp.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}),
    )


def test_comments_normalizes_inline_comment(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/owner/repo/pulls/42/reviews"],
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/owner/repo/pulls/42/comments"],
        stdout=json.dumps(
            [
                {
                    "id": 111,
                    "path": "app/x.py",
                    "line": 42,
                    "body": "fix this",
                    "user": {"login": "reviewer"},
                    "created_at": "2026-04-23T10:00:00Z",
                    "pull_request_review_id": 999,
                }
            ]
        ),
    )
    _register_graphql_empty_threads(fake_proc)

    result = cli.invoke(app, ["comments", "--repo", "owner/repo", "--pr", "42"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data == [
        {
            "id": 111,
            "thread_id": None,
            "kind": "inline",
            "path": "app/x.py",
            "line": 42,
            "body": "fix this",
            "author": "reviewer",
            "created_at": "2026-04-23T10:00:00Z",
            "reply_url": "repos/owner/repo/pulls/42/comments/111/replies",
            "is_resolved": False,
        }
    ]


def test_comments_includes_review_body_and_thread_id(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/o/r/pulls/1/reviews"],
        stdout=json.dumps(
            [
                {
                    "id": 999,
                    "body": "overall looks good",
                    "user": {"login": "r"},
                    "submitted_at": "2026-04-23T11:00:00Z",
                    "state": "COMMENTED",
                },
                {
                    "id": 1000,
                    "body": "",
                    "user": {"login": "r"},
                    "submitted_at": "2026-04-23T12:00:00Z",
                    "state": "APPROVED",
                },
            ]
        ),
    )
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/o/r/pulls/1/comments"],
        stdout=json.dumps(
            [
                {
                    "id": 111,
                    "path": "a.py",
                    "line": 5,
                    "body": "b",
                    "user": {"login": "r"},
                    "created_at": "2026-04-23T10:00:00Z",
                    "pull_request_review_id": 999,
                }
            ]
        ),
    )
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_abc",
                                        "isResolved": True,
                                        "comments": {"nodes": [{"databaseId": 111}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        ),
    )

    result = cli.invoke(app, ["comments", "--repo", "o/r", "--pr", "1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    # inline first, then review body; empty review body is filtered
    assert [c["kind"] for c in data] == ["inline", "review_body"]
    assert data[0]["thread_id"] == "PRRT_abc"
    assert data[0]["is_resolved"] is True
    assert data[1]["body"] == "overall looks good"
    assert data[1]["reply_url"] is None
