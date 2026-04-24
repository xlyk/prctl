"""Tests for the local ``notes`` subsystem + ``queue --with-notes``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from prctl import notes
from prctl.cli import app
from tests.conftest import FakeProc


@pytest.fixture
def notes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRCTL_NOTES_ROOT", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


# ---------- pure module ----------


def test_get_note_returns_skeleton_when_missing(notes_root: Path) -> None:
    note = notes.get_note("foo/bar", 7)
    assert note["schema_version"] == 1
    assert note["pr"] == "foo/bar#7"
    assert note["summary"] is None
    assert note["sessions"] == []
    assert note["awaiting_author_on"] == []
    # skeleton must not write a file
    assert list(notes_root.iterdir()) == []


def test_set_summary_writes_file(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 7, sha="abc", intent="does X", scope=["src/a.py", "tests/t.py"])
    loaded = notes.get_note("foo/bar", 7)
    assert loaded["summary"] == {"sha": "abc", "intent": "does X", "scope": ["src/a.py", "tests/t.py"]}


def test_append_session_generates_id(notes_root: Path) -> None:
    out = notes.append_session("foo/bar", 7, {"head_sha": "abc", "user_verdict": "ready"})
    assert out["session_id"].startswith("sess_")
    loaded = notes.get_note("foo/bar", 7)
    assert len(loaded["sessions"]) == 1
    assert loaded["sessions"][0]["user_verdict"] == "ready"
    assert loaded["sessions"][0]["head_sha"] == "abc"
    assert loaded["sessions"][0]["posted"] == []
    assert loaded["sessions"][0]["dropped"] == []


def test_append_session_respects_provided_id(notes_root: Path) -> None:
    notes.append_session("foo/bar", 7, {"id": "sess_custom", "head_sha": "abc"})
    loaded = notes.get_note("foo/bar", 7)
    assert loaded["sessions"][0]["id"] == "sess_custom"


def test_track_thread_replaces_existing(notes_root: Path) -> None:
    notes.track_thread("foo/bar", 7, thread_id="PRRT_1", note_text="first", sha="aaa")
    notes.track_thread("foo/bar", 7, thread_id="PRRT_1", note_text="second", sha="bbb")
    loaded = notes.get_note("foo/bar", 7)
    assert len(loaded["awaiting_author_on"]) == 1
    assert loaded["awaiting_author_on"][0]["note"] == "second"
    assert loaded["awaiting_author_on"][0]["sha_at_record"] == "bbb"


def test_untrack_thread_removes(notes_root: Path) -> None:
    notes.track_thread("foo/bar", 7, thread_id="PRRT_1", note_text="x", sha="a")
    notes.track_thread("foo/bar", 7, thread_id="PRRT_2", note_text="y", sha="a")
    result = notes.untrack_thread("foo/bar", 7, thread_id="PRRT_1")
    assert result["removed"] is True
    loaded = notes.get_note("foo/bar", 7)
    assert len(loaded["awaiting_author_on"]) == 1
    assert loaded["awaiting_author_on"][0]["thread_id"] == "PRRT_2"


def test_untrack_thread_missing_is_noop(notes_root: Path) -> None:
    out = notes.untrack_thread("foo/bar", 7, thread_id="never_tracked")
    assert out["removed"] is False


def test_list_notes_filters_by_repo(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 1, sha="a", intent="i1")
    notes.set_summary("foo/bar", 2, sha="a", intent="i2")
    notes.set_summary("baz/qux", 3, sha="a", intent="i3")
    result = notes.list_notes("foo/bar")
    assert {n["pr"] for n in result} == {"foo/bar#1", "foo/bar#2"}


def test_list_notes_unfiltered_returns_all(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 1, sha="a", intent="i1")
    notes.set_summary("baz/qux", 3, sha="a", intent="i3")
    result = notes.list_notes()
    assert len(result) == 2


def test_list_notes_missing_root_returns_empty(notes_root: Path) -> None:
    # notes_root exists but is empty
    assert notes.list_notes() == []


def test_atomic_write_leaves_no_tmp(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 7, sha="a", intent="i")
    notes.append_session("foo/bar", 7, {"head_sha": "a"})
    leftovers = list(notes_root.rglob("*.tmp*"))
    assert leftovers == []


def test_unknown_schema_version_raises(notes_root: Path) -> None:
    path = notes.note_path("foo/bar", 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "pr": "foo/bar#7"}))
    with pytest.raises(ValueError, match="unknown schema_version"):
        notes.get_note("foo/bar", 7)


def test_note_path_uses_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRCTL_NOTES_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    expected = tmp_path / "prctl" / "notes" / "foo__bar__7.json"
    assert notes.note_path("foo/bar", 7) == expected


def test_compact_for_queue(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 7, sha="abc", intent="does X", scope=[])
    notes.track_thread("foo/bar", 7, thread_id="PRRT_1", note_text="x", sha="abc")
    notes.append_session("foo/bar", 7, {"head_sha": "def", "user_verdict": "ok"})
    loaded = notes.try_load_note("foo/bar", 7)
    compact = notes.compact_for_queue(loaded)
    assert compact == {
        "intent": "does X",
        "intent_sha": "abc",
        "awaiting_count": 1,
        "last_session_sha": "def",
        "last_session_ts": loaded["sessions"][-1]["ts"],
    }


def test_compact_for_queue_none_passthrough() -> None:
    assert notes.compact_for_queue(None) is None


def test_try_load_note_returns_none_when_missing(notes_root: Path) -> None:
    assert notes.try_load_note("foo/bar", 99) is None


# ---------- CLI ----------


def test_cli_get_empty(notes_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["notes", "get", "--repo", "foo/bar", "--pr", "7"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["pr"] == "foo/bar#7"
    assert data["summary"] is None


def test_cli_set_and_get_roundtrip(notes_root: Path) -> None:
    runner = CliRunner()
    r1 = runner.invoke(
        app,
        [
            "notes",
            "set",
            "--repo",
            "foo/bar",
            "--pr",
            "7",
            "--summary-sha",
            "deadbeef",
            "--summary-intent",
            "Adds X to Y",
            "--summary-scope",
            "src/a.py",
            "--summary-scope",
            "tests/t.py",
        ],
    )
    assert r1.exit_code == 0, r1.stdout
    r2 = runner.invoke(app, ["notes", "get", "--repo", "foo/bar", "--pr", "7"])
    data = json.loads(r2.stdout)
    assert data["summary"] == {
        "sha": "deadbeef",
        "intent": "Adds X to Y",
        "scope": ["src/a.py", "tests/t.py"],
    }


def test_cli_append_via_file(notes_root: Path, tmp_path: Path) -> None:
    session_file = tmp_path / "sess.json"
    session_file.write_text(json.dumps({"head_sha": "abc", "user_verdict": "skip both"}))
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["notes", "append", "--repo", "foo/bar", "--pr", "7", "--session", f"@{session_file}"],
    )
    assert result.exit_code == 0, result.stdout
    note = notes.get_note("foo/bar", 7)
    assert note["sessions"][0]["user_verdict"] == "skip both"


def test_cli_track_and_untrack(notes_root: Path) -> None:
    runner = CliRunner()
    r1 = runner.invoke(
        app,
        [
            "notes",
            "track-thread",
            "--repo",
            "foo/bar",
            "--pr",
            "7",
            "--thread-id",
            "PRRT_abc",
            "--note",
            "expects fix in X",
            "--sha",
            "deadbeef",
        ],
    )
    assert r1.exit_code == 0, r1.stdout
    r2 = runner.invoke(
        app,
        ["notes", "untrack-thread", "--repo", "foo/bar", "--pr", "7", "--thread-id", "PRRT_abc"],
    )
    assert r2.exit_code == 0, r2.stdout
    out = json.loads(r2.stdout)
    assert out["removed"] is True


def test_cli_list_filters(notes_root: Path) -> None:
    notes.set_summary("foo/bar", 1, sha="a", intent="i")
    notes.set_summary("baz/qux", 2, sha="a", intent="i")
    runner = CliRunner()
    result = runner.invoke(app, ["notes", "list", "--repo", "foo/bar"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert [n["pr"] for n in data] == ["foo/bar#1"]


def test_cli_path_returns_plain_string(notes_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["notes", "path", "--repo", "foo/bar", "--pr", "7"])
    assert result.exit_code == 0
    assert result.stdout.strip() == str(notes.note_path("foo/bar", 7))


def test_cli_migrate_reports_schema_version(notes_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["notes", "migrate"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"schema_version": 1, "migrated": 0}


# ---------- queue --with-notes integration ----------


def _queue_pr(number: int, mergeable: str = "MERGEABLE") -> dict[str, object]:
    return {
        "number": number,
        "title": f"t{number}",
        "author": {"login": "other"},
        "isDraft": False,
        "reviewDecision": "",
        "mergeable": mergeable,
        "headRefName": f"h{number}",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }


def test_queue_with_notes_attaches_compact(
    notes_root: Path,
    fake_proc: FakeProc,
) -> None:
    # one PR exists on gh side; caller is not its author
    fake_proc.register(("gh", "api", "user"), stdout=json.dumps({"login": "reviewer"}))
    fake_proc.register(
        ("gh", "pr", "list", "--repo", "foo/bar"),
        stdout=json.dumps([_queue_pr(42)]),
    )
    fake_proc.register(
        ("gh", "api", "repos/foo/bar/pulls/42/commits"),
        stdout=json.dumps([{"commit": {"committer": {"date": "2026-04-24T10:00:00Z"}}}]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/reviews"),
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/comments"),
        stdout=json.dumps([]),
    )

    notes.set_summary("foo/bar", 42, sha="aaa", intent="the gist", scope=[])
    notes.track_thread("foo/bar", 42, thread_id="PRRT_1", note_text="fix x", sha="aaa")

    runner = CliRunner()
    result = runner.invoke(app, ["queue", "--repos", "foo/bar", "--with-notes"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    # caller never engaged → initial_review bucket
    pr = data["initial_review"][0]
    assert pr["notes"] == {
        "intent": "the gist",
        "intent_sha": "aaa",
        "awaiting_count": 1,
        "last_session_sha": None,
        "last_session_ts": None,
    }


def test_queue_without_notes_flag_omits_notes_key(
    notes_root: Path,
    fake_proc: FakeProc,
) -> None:
    fake_proc.register(("gh", "api", "user"), stdout=json.dumps({"login": "reviewer"}))
    fake_proc.register(
        ("gh", "pr", "list", "--repo", "foo/bar"),
        stdout=json.dumps([_queue_pr(42)]),
    )
    fake_proc.register(
        ("gh", "api", "repos/foo/bar/pulls/42/commits"),
        stdout=json.dumps([{"commit": {"committer": {"date": "2026-04-24T10:00:00Z"}}}]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/reviews"),
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/comments"),
        stdout=json.dumps([]),
    )

    notes.set_summary("foo/bar", 42, sha="aaa", intent="the gist")

    runner = CliRunner()
    result = runner.invoke(app, ["queue", "--repos", "foo/bar"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    pr = data["initial_review"][0]
    assert "notes" not in pr


def test_queue_with_notes_null_when_no_note(
    notes_root: Path,
    fake_proc: FakeProc,
) -> None:
    fake_proc.register(("gh", "api", "user"), stdout=json.dumps({"login": "reviewer"}))
    fake_proc.register(
        ("gh", "pr", "list", "--repo", "foo/bar"),
        stdout=json.dumps([_queue_pr(42)]),
    )
    fake_proc.register(
        ("gh", "api", "repos/foo/bar/pulls/42/commits"),
        stdout=json.dumps([{"commit": {"committer": {"date": "2026-04-24T10:00:00Z"}}}]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/reviews"),
        stdout=json.dumps([]),
    )
    fake_proc.register(
        ("gh", "api", "--paginate", "repos/foo/bar/pulls/42/comments"),
        stdout=json.dumps([]),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["queue", "--repos", "foo/bar", "--with-notes"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    pr = data["initial_review"][0]
    assert pr["notes"] is None
