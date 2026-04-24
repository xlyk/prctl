"""Local PR notes — context that survives between review sessions.

A small filesystem-backed JSON store: one file per PR at
``~/.config/prctl/notes/<owner>__<repo>__<pr>.json``. Captures what the PR
is trying to do, which review threads the caller is waiting on the author
to address, and per-session records of what was posted, what was dropped,
and what the user decided.

Atomic writes (tmp + rename). No locking — single-writer by design.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_ENV_ROOT = "PRCTL_NOTES_ROOT"


def notes_root() -> Path:
    """Directory containing per-PR note files. Override via ``$PRCTL_NOTES_ROOT``."""
    override = os.environ.get(_ENV_ROOT)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "prctl" / "notes"


def _slug(repo: str, pr: int) -> str:
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}__{pr}.json"


def note_path(repo: str, pr: int) -> Path:
    return notes_root() / _slug(repo, pr)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _new_session_id() -> str:
    return f"sess_{secrets.token_hex(6)}"


def _empty_note(repo: str, pr: int) -> dict[str, Any]:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "pr": f"{repo}#{pr}",
        "created_at": now,
        "updated_at": now,
        "summary": None,
        "awaiting_author_on": [],
        "sessions": [],
    }


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unknown schema_version {version!r} in {path}; run `prctl notes migrate` or delete the file")
    return data


def try_load_note(repo: str, pr: int) -> dict[str, Any] | None:
    """Return the note if the file exists, else ``None``."""
    return _load(note_path(repo, pr))


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ensure_loaded(repo: str, pr: int) -> dict[str, Any]:
    loaded = _load(note_path(repo, pr))
    return loaded if loaded is not None else _empty_note(repo, pr)


def get_note(repo: str, pr: int) -> dict[str, Any]:
    """Return the note for ``(repo, pr)``, or an empty-note skeleton if absent."""
    return _ensure_loaded(repo, pr)


def set_summary(
    repo: str,
    pr: int,
    *,
    sha: str,
    intent: str,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """Set or replace the PR summary block."""
    note = _ensure_loaded(repo, pr)
    note["summary"] = {
        "sha": sha,
        "intent": intent,
        "scope": list(scope or []),
    }
    note["updated_at"] = _now()
    path = note_path(repo, pr)
    _atomic_write(path, note)
    return {"path": str(path)}


def append_session(repo: str, pr: int, session: dict[str, Any]) -> dict[str, Any]:
    """Append a review session record. ``session`` is free-form; known keys are
    ``id``, ``ts``, ``head_sha``, ``posted``, ``dropped``, ``user_verdict``."""
    note = _ensure_loaded(repo, pr)
    session_id = session.get("id") or _new_session_id()
    entry = {
        "id": session_id,
        "ts": session.get("ts") or _now(),
        "head_sha": session.get("head_sha"),
        "posted": list(session.get("posted") or []),
        "dropped": list(session.get("dropped") or []),
        "user_verdict": session.get("user_verdict"),
    }
    note["sessions"].append(entry)
    note["updated_at"] = _now()
    path = note_path(repo, pr)
    _atomic_write(path, note)
    return {"session_id": session_id, "path": str(path)}


def track_thread(
    repo: str,
    pr: int,
    *,
    thread_id: str,
    note_text: str,
    sha: str,
) -> dict[str, Any]:
    """Record a review thread we're waiting on the author to address.

    Re-tracking a thread replaces the prior entry — last record wins.
    """
    note = _ensure_loaded(repo, pr)
    kept = [e for e in note["awaiting_author_on"] if e.get("thread_id") != thread_id]
    kept.append(
        {
            "thread_id": thread_id,
            "note": note_text,
            "sha_at_record": sha,
            "recorded_at": _now(),
        }
    )
    note["awaiting_author_on"] = kept
    note["updated_at"] = _now()
    path = note_path(repo, pr)
    _atomic_write(path, note)
    return {"tracked": thread_id, "path": str(path)}


def untrack_thread(repo: str, pr: int, *, thread_id: str) -> dict[str, Any]:
    """Stop tracking a thread. Idempotent — missing IDs are a no-op."""
    note = _ensure_loaded(repo, pr)
    before = len(note["awaiting_author_on"])
    note["awaiting_author_on"] = [e for e in note["awaiting_author_on"] if e.get("thread_id") != thread_id]
    removed = before != len(note["awaiting_author_on"])
    note["updated_at"] = _now()
    path = note_path(repo, pr)
    _atomic_write(path, note)
    return {"untracked": thread_id, "removed": removed, "path": str(path)}


def list_notes(repo_filter: str | None = None) -> list[dict[str, Any]]:
    """Enumerate notes on disk (optionally filtered to one ``owner/name``)."""
    root = notes_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = _load(entry)
        except (ValueError, json.JSONDecodeError):
            continue
        if data is None:
            continue
        pr_label = data.get("pr", "")
        if repo_filter and not pr_label.startswith(f"{repo_filter}#"):
            continue
        sessions = data.get("sessions") or []
        last = sessions[-1] if sessions else {}
        summary = data.get("summary") or {}
        out.append(
            {
                "pr": pr_label,
                "updated_at": data.get("updated_at"),
                "open_threads": len(data.get("awaiting_author_on") or []),
                "last_session_sha": last.get("head_sha"),
                "last_session_ts": last.get("ts"),
                "summary_sha": summary.get("sha"),
            }
        )
    return out


def compact_for_queue(note: dict[str, Any] | None) -> dict[str, Any] | None:
    """Condensed view of a note for embedding in ``queue`` output."""
    if note is None:
        return None
    summary = note.get("summary") or {}
    sessions = note.get("sessions") or []
    last = sessions[-1] if sessions else {}
    return {
        "intent": summary.get("intent"),
        "intent_sha": summary.get("sha"),
        "awaiting_count": len(note.get("awaiting_author_on") or []),
        "last_session_sha": last.get("head_sha"),
        "last_session_ts": last.get("ts"),
    }
