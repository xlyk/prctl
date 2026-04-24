"""Tests for ``reply`` and ``resolve-thread`` subcommands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc


def test_reply_single_comment(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/comments/123/replies", "--method", "POST"],
        stdout=json.dumps({"id": 9001}),
    )

    result = cli.invoke(
        app,
        [
            "reply",
            "--repo",
            "o/r",
            "--pr",
            "1",
            "--comment-id",
            "123",
            "--body",
            "fixed — added await",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"posted": [9001], "resolved": []}

    # body passed via -f body=...
    post = [c for c in fake_proc.calls if "--method" in c][0]
    assert "-f" in post
    body_field = post[post.index("-f") + 1]
    assert body_field == "body=fixed — added await"


def test_reply_batch(fake_proc: FakeProc, cli: CliRunner, tmp_path: Path) -> None:
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/comments/123/replies", "--method", "POST"],
        stdout=json.dumps({"id": 1001}),
    )
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/comments/456/replies", "--method", "POST"],
        stdout=json.dumps({"id": 1002}),
    )
    batch = tmp_path / "replies.json"
    batch.write_text(json.dumps([{"comment_id": 123, "body": "a"}, {"comment_id": 456, "body": "b"}]))

    result = cli.invoke(
        app,
        ["reply", "--repo", "o/r", "--pr", "1", "--batch", str(batch)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"posted": [1001, 1002], "resolved": []}


def test_resolve_thread(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_abc", "isResolved": True}}}}),
    )

    result = cli.invoke(app, ["resolve-thread", "PRRT_abc"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"thread_id": "PRRT_abc", "state": "resolved"}


def test_reply_batch_with_inline_resolve(fake_proc: FakeProc, cli: CliRunner, tmp_path: Path) -> None:
    """A batch entry may carry a ``resolve`` field → reply first, then resolve that thread."""
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/comments/123/replies", "--method", "POST"],
        stdout=json.dumps({"id": 2001}),
    )
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/comments/456/replies", "--method", "POST"],
        stdout=json.dumps({"id": 2002}),
    )
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_a", "isResolved": True}}}}),
    )

    batch = tmp_path / "replies.json"
    batch.write_text(
        json.dumps(
            [
                {"comment_id": 123, "body": "fixed", "resolve": "PRRT_a"},
                {"comment_id": 456, "body": "declined — out of scope"},
            ]
        )
    )

    result = cli.invoke(app, ["reply", "--repo", "o/r", "--pr", "1", "--batch", str(batch)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"posted": [2001, 2002], "resolved": ["PRRT_a"]}

    # reply for 123 precedes its resolve-thread call
    post_123_idx = next(i for i, c in enumerate(fake_proc.calls) if c[2] == "repos/o/r/pulls/1/comments/123/replies")
    graphql_idx = next(i for i, c in enumerate(fake_proc.calls) if c[:3] == ["gh", "api", "graphql"])
    assert post_123_idx < graphql_idx
