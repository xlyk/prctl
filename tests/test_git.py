"""Tests for the _git subprocess helper."""

from __future__ import annotations

from prctl.cli import _git
from tests.conftest import FakeProc


def test_git_returns_stripped_stdout(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["git", "rev-parse", "HEAD"],
        stdout="abc1234\n",
    )
    assert _git("rev-parse", "HEAD") == "abc1234"
    assert fake_proc.calls[0] == ["git", "rev-parse", "HEAD"]
