"""Test harness for prctl.

Provides a ``fake_proc`` fixture that intercepts ``subprocess.run`` calls
inside the helper, keyed on argv prefix. Tests register canonical responses;
the helper sees them as if they came from ``gh`` / ``git``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner


@dataclass
class _Canned:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class FakeProc:
    """Registers canned subprocess responses by argv prefix.

    Matching is longest-prefix-wins so specific registrations beat generic ones.
    Unknown argv raises loudly — better than silently returning empty output.
    """

    responses: list[tuple[tuple[str, ...], _Canned]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def register(
        self,
        argv_prefix: Iterable[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.responses.append((tuple(argv_prefix), _Canned(stdout=stdout, stderr=stderr, returncode=returncode)))

    def _match(self, argv: list[str]) -> _Canned:
        best: tuple[int, _Canned] | None = None
        for prefix, canned in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > best[0]):
                best = (len(prefix), canned)
        if best is None:
            raise AssertionError(f"FakeProc: no registration for argv {argv}")
        return best[1]

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        canned = self._match(argv)
        completed = subprocess.CompletedProcess(
            args=argv, returncode=canned.returncode, stdout=canned.stdout, stderr=canned.stderr
        )
        if canned.returncode != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(
                returncode=canned.returncode, cmd=argv, output=canned.stdout, stderr=canned.stderr
            )
        return completed


@pytest.fixture
def fake_proc(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    fp = FakeProc()
    import prctl.cli as mod

    monkeypatch.setattr(mod.subprocess, "run", fp.run)
    return fp


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()
