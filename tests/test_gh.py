"""Tests for the thin gh/git subprocess helpers in scripts/pr_helper."""

from __future__ import annotations

import pytest

from prctl.cli import _gh
from tests.conftest import FakeProc


def test_gh_api_returns_parsed_json(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["gh", "api", "user"],
        stdout='{"login": "octocat"}',
    )
    assert _gh("user") == {"login": "octocat"}
    assert fake_proc.calls[0][:3] == ["gh", "api", "user"]


def test_gh_api_paginate_concatenates_arrays(fake_proc: FakeProc) -> None:
    """With paginate=True the helper passes --paginate and expects gh to emit
    a single concatenated JSON array (gh --paginate behaviour)."""
    fake_proc.register(
        ["gh", "api", "--paginate", "repos/o/r/pulls/1/comments"],
        stdout='[{"id": 1}, {"id": 2}]',
    )
    result = _gh("repos/o/r/pulls/1/comments", paginate=True)
    assert result == [{"id": 1}, {"id": 2}]
    assert "--paginate" in fake_proc.calls[0]


def test_gh_api_jq_filter_emits_raw(fake_proc: FakeProc) -> None:
    """When a jq filter is supplied, the output is returned as-is (string)."""
    fake_proc.register(
        ["gh", "api", "user", "--jq", ".login"],
        stdout="octocat\n",
    )
    assert _gh("user", jq=".login") == "octocat"


def test_gh_api_raises_on_failure(fake_proc: FakeProc) -> None:
    fake_proc.register(
        ["gh", "api", "nope"],
        stdout="",
        stderr="gh: not found",
        returncode=1,
    )
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        _gh("nope")
