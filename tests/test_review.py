"""Tests for ``validate-review`` and ``post-review`` subcommands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc

DIFF = """\
diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x
+y
 z
"""


def test_validate_review_accepts_payload_with_valid_lines(fake_proc: FakeProc, cli: CliRunner, tmp_path: Path) -> None:
    fake_proc.register(["gh", "pr", "diff", "--repo", "o/r", "1"], stdout=DIFF)
    payload = tmp_path / "review.json"
    payload.write_text(
        json.dumps(
            {
                "commit_id": "abc",
                "event": "COMMENT",
                "body": "",
                "comments": [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "suggestion: ..."}],
            }
        )
    )

    result = cli.invoke(app, ["validate-review", str(payload), "--repo", "o/r", "--pr", "1"])
    assert result.exit_code == 0, result.output


def test_validate_review_rejects_line_not_in_diff(fake_proc: FakeProc, cli: CliRunner, tmp_path: Path) -> None:
    fake_proc.register(["gh", "pr", "diff", "--repo", "o/r", "1"], stdout=DIFF)
    payload = tmp_path / "review.json"
    payload.write_text(
        json.dumps(
            {
                "commit_id": "abc",
                "event": "COMMENT",
                "body": "",
                "comments": [
                    {"path": "a.py", "line": 99, "side": "RIGHT", "body": "x"},
                    {"path": "a.py", "line": 2, "side": "RIGHT", "body": "ok"},
                ],
            }
        )
    )

    result = cli.invoke(app, ["validate-review", str(payload), "--repo", "o/r", "--pr", "1"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["offenders"] == [{"path": "a.py", "line": 99, "reason": "not an added/modified line in the PR diff"}]


def test_post_review_validates_then_posts(fake_proc: FakeProc, cli: CliRunner, tmp_path: Path) -> None:
    fake_proc.register(["gh", "pr", "diff", "--repo", "o/r", "1"], stdout=DIFF)
    fake_proc.register(
        ["gh", "api", "repos/o/r/pulls/1/reviews", "--method", "POST"],
        stdout=json.dumps({"id": 777, "html_url": "https://github.com/o/r/pull/1#pullrequestreview-777"}),
    )
    payload = tmp_path / "review.json"
    payload.write_text(
        json.dumps(
            {
                "commit_id": "abc",
                "event": "COMMENT",
                "body": "",
                "comments": [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "ok"}],
            }
        )
    )

    result = cli.invoke(app, ["post-review", str(payload), "--repo", "o/r", "--pr", "1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "id": 777,
        "url": "https://github.com/o/r/pull/1#pullrequestreview-777",
    }

    # verify the POST invocation carried the payload as --input
    post_call = [c for c in fake_proc.calls if c[:2] == ["gh", "api"] and "--method" in c][0]
    assert "POST" in post_call
    assert "--input" in post_call
