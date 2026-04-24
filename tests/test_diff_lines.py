"""Tests for the ``diff-lines`` subcommand."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from prctl.cli import app
from tests.conftest import FakeProc

DIFF_SIMPLE = """\
diff --git a/app/x.py b/app/x.py
index 1111111..2222222 100644
--- a/app/x.py
+++ b/app/x.py
@@ -1,3 +1,5 @@
 import os
+import sys
+import json

 print("hi")
"""


def test_diff_lines_collects_added_lines(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["gh", "pr", "diff", "--repo", "o/r", "1"], stdout=DIFF_SIMPLE)

    result = cli.invoke(app, ["diff-lines", "--repo", "o/r", "--pr", "1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"app/x.py": [2, 3]}


DIFF_MULTI_HUNK = """\
diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
+y = 2
 z = 3
@@ -10,2 +11,4 @@
 q
+r
+s
 t
diff --git a/b.py b/b.py
new file mode 100644
--- /dev/null
+++ b/b.py
@@ -0,0 +1,2 @@
+alpha
+beta
"""


def test_diff_lines_handles_multiple_hunks_and_new_files(fake_proc: FakeProc, cli: CliRunner) -> None:
    fake_proc.register(["gh", "pr", "diff", "--repo", "o/r", "42"], stdout=DIFF_MULTI_HUNK)

    result = cli.invoke(app, ["diff-lines", "--repo", "o/r", "--pr", "42"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"a.py": [2, 12, 13], "b.py": [1, 2]}
