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
    assert json.loads(result.stdout) == {"posted": [9001]}

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
    assert json.loads(result.stdout) == {"posted": [1001, 1002]}


def test_resolve_thread(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(
        ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_abc", "isResolved": True}}}}),
    )

    result = cli.invoke(app, ["resolve-thread", "PRRT_abc"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"thread_id": "PRRT_abc", "state": "resolved"}
