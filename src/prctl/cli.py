"""prctl — PR-workflow helper CLI.

Mechanical building blocks for PR review / merge workflows:

  - fetch/normalize review + inline comments
  - parse PR diffs into {path: [head_lines]}
  - validate and post review payloads
  - reply to comments, resolve threads
  - categorize the PR review queue
  - discover stacked-PR order
  - safe squash-merge (children-aware)
  - rebase-onto-main with stale-parent detection

Thin shell over ``gh`` and ``git``. JSON on stdout by default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from prctl import notes

app = typer.Typer(
    help="prctl — PR-workflow helper.",
    no_args_is_help=True,
)

notes_app = typer.Typer(
    help="Local PR notes — context that survives between review sessions.",
    no_args_is_help=True,
)
app.add_typer(notes_app, name="notes")


@app.callback()
def _root() -> None:
    """Root callback — forces multi-command mode so subcommand names are required."""


def _git(*args: str) -> str:
    """Call ``git <args>`` and return stripped stdout."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return result.stdout.rstrip("\n")


def _gh(path: str, *, paginate: bool = False, jq: str | None = None) -> Any:
    """Call ``gh api <path>`` and return parsed JSON (or raw string if ``jq`` is set)."""
    argv = ["gh", "api"]
    if paginate:
        argv.append("--paginate")
    argv.append(path)
    if jq is not None:
        argv.extend(["--jq", jq])
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    if jq is not None:
        return result.stdout.rstrip("\n")
    return json.loads(result.stdout)


REPO_OPT = Annotated[str | None, typer.Option("--repo", help="owner/name; default: current PR")]
PR_OPT = Annotated[int | None, typer.Option("--pr", help="PR number; default: current branch's PR")]


def _emit(payload: Any) -> None:
    """Print JSON to stdout without trailing whitespace."""
    typer.echo(json.dumps(payload, indent=2, sort_keys=False))


def _resolve_pr(repo: str | None, pr: int | None) -> tuple[str, int]:
    """Resolve (owner/repo, pr_number) — falling back to the current branch's PR."""
    if repo and pr:
        return repo, pr
    view = _gh_pr_view(["number", "headRepositoryOwner", "headRepository", "baseRepository"])
    if repo is None:
        base = view.get("baseRepository") or {}
        owner = (view.get("headRepositoryOwner") or base.get("owner") or {}).get("login")
        name = (view.get("headRepository") or base).get("name")
        if not owner or not name:
            raise typer.BadParameter("could not determine repo; pass --repo")
        repo = f"{owner}/{name}"
    if pr is None:
        pr = int(view["number"])
    return repo, pr


def _gh_pr_view(fields: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "pr", "view", "--json", ",".join(fields)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _review_threads(owner: str, name: str, number: int) -> dict[int, dict[str, Any]]:
    """Map inline-comment id → {thread_id, is_resolved} via GraphQL."""
    query = (
        "query($o:String!,$n:String!,$num:Int!){"
        "repository(owner:$o,name:$n){pullRequest(number:$num){"
        "reviewThreads(first:100){nodes{id isResolved comments(first:50){nodes{databaseId}}}}"
        "}}}"
    )
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"o={owner}",
        "-F",
        f"n={name}",
        "-F",
        f"num={number}",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    out: dict[int, dict[str, Any]] = {}
    nodes = (
        payload.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    ) or []
    for thread in nodes:
        thread_id = thread["id"]
        resolved = bool(thread.get("isResolved"))
        for c in thread.get("comments", {}).get("nodes", []) or []:
            out[c["databaseId"]] = {"thread_id": thread_id, "is_resolved": resolved}
    return out


_PASS_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
_FAIL_CONCLUSIONS = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"}


def categorize_pr(
    pr: dict[str, Any],
    *,
    last_commit: str | None,
    last_caller_feedback: str | None,
    unresolved_caller_threads: int | None = None,
) -> str:
    """Decide which bucket a PR belongs to. Pure — no I/O.

    Returns one of: ``ready_merge``, ``initial_review``, ``another_round``,
    ``awaiting_author``.

    When ``unresolved_caller_threads`` is supplied and is 0, a PR that would
    otherwise land in ``another_round`` is demoted to ``awaiting_author`` —
    the new commits didn't leave any caller thread unresolved, so there is
    nothing actionable for the caller.
    """
    checks = pr.get("statusCheckRollup_conclusions") or []
    ci_pass = all(c in _PASS_CONCLUSIONS for c in checks)
    ci_failed = any(c in _FAIL_CONCLUSIONS for c in checks)
    rd = pr.get("reviewDecision", "")
    mergeable = pr.get("mergeable", "")
    if rd == "APPROVED" and mergeable == "MERGEABLE" and ci_pass and not ci_failed:
        return "ready_merge"
    if last_caller_feedback is None:
        return "initial_review"
    if last_commit is not None and last_commit > last_caller_feedback:
        if unresolved_caller_threads == 0:
            return "awaiting_author"
        return "another_round"
    return "awaiting_author"


def _unresolved_caller_threads(owner: str, name: str, number: int, caller: str) -> int:
    """Count review threads where ``caller`` commented and the thread is still open.

    A thread is counted iff: (a) at least one comment is authored by ``caller``,
    (b) ``isResolved`` is false, and (c) ``isOutdated`` is false. Outdated threads
    are treated as implicitly addressed — the code the comment anchored to has
    since moved.
    """
    query = (
        "query($o:String!,$n:String!,$num:Int!){"
        "repository(owner:$o,name:$n){pullRequest(number:$num){"
        "reviewThreads(first:100){nodes{isResolved isOutdated "
        "comments(first:50){nodes{author{login}}}}}"
        "}}}"
    )
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"o={owner}",
        "-F",
        f"n={name}",
        "-F",
        f"num={number}",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    nodes = (
        payload.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    ) or []
    count = 0
    for thread in nodes:
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        authors = {(c.get("author") or {}).get("login") for c in (thread.get("comments") or {}).get("nodes", []) or []}
        if caller in authors:
            count += 1
    return count


def _parse_unified_diff(diff_text: str) -> dict[str, list[int]]:
    """Return {path: [head_line_numbers_with_additions]} from unified diff text."""
    out: dict[str, list[int]] = {}
    path: str | None = None
    line_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                out.setdefault(path, [])
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@  (d may be omitted for single-line hunks)
            try:
                plus = raw.split("+", 1)[1].split(" ", 1)[0]
                line_no = int(plus.split(",", 1)[0]) - 1
            except (IndexError, ValueError):
                line_no = 0
            continue
        if path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line_no += 1
            out[path].append(line_no)
        elif raw.startswith(" "):
            line_no += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass  # deletion — doesn't advance new-file line counter
    return out


def _gh_pr_diff(repo: str | None, pr: int | None) -> str:
    argv = ["gh", "pr", "diff"]
    if repo:
        argv.extend(["--repo", repo])
    if pr is not None:
        argv.append(str(pr))
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    return result.stdout


def _validate_payload(payload: dict[str, Any], lines_by_path: dict[str, list[int]]) -> list[dict[str, Any]]:
    """Return a list of offenders — each entry describes one out-of-range (path, line)."""
    offenders: list[dict[str, Any]] = []
    for c in payload.get("comments", []) or []:
        path = c.get("path")
        if path is None:
            offenders.append({"path": None, "line": None, "reason": "missing path"})
            continue
        valid = set(lines_by_path.get(path, []))
        for key in ("line", "start_line"):
            line = c.get(key)
            if line is None:
                continue
            if line not in valid:
                offenders.append(
                    {
                        "path": path,
                        "line": line,
                        "reason": "not an added/modified line in the PR diff",
                    }
                )
    return offenders


@app.command("validate-review")
def cmd_validate_review(
    payload_path: Annotated[Path, typer.Argument(help="path to review-payload.json")],
    repo: REPO_OPT = None,
    pr: PR_OPT = None,
) -> None:
    """Verify every (path, line) in the payload appears in the PR diff."""
    payload = json.loads(payload_path.read_text())
    diff = _gh_pr_diff(repo, pr)
    offenders = _validate_payload(payload, _parse_unified_diff(diff))
    if offenders:
        _emit({"offenders": offenders})
        raise typer.Exit(code=2)


def _post_reply(full_repo: str, number: int, comment_id: int, body: str) -> int:
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{full_repo}/pulls/{number}/comments/{comment_id}/replies",
            "--method",
            "POST",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(json.loads(result.stdout)["id"])


def _resolve_thread_once(thread_id: str) -> None:
    mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}"
    subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={mutation}", "-F", f"id={thread_id}"],
        capture_output=True,
        text=True,
        check=True,
    )


@app.command("reply")
def cmd_reply(
    repo: REPO_OPT = None,
    pr: PR_OPT = None,
    comment_id: Annotated[int | None, typer.Option("--comment-id")] = None,
    body: Annotated[str | None, typer.Option("--body")] = None,
    batch: Annotated[Path | None, typer.Option("--batch", help="path to JSON batch file")] = None,
) -> None:
    """Post one inline reply or a batch. Batch entries may carry a ``resolve`` field
    (thread node ID) to resolve the thread atomically after the reply lands."""
    full_repo, number = _resolve_pr(repo, pr)
    posted: list[int] = []
    resolved: list[str] = []
    if batch is not None:
        entries = json.loads(batch.read_text())
        for entry in entries:
            posted.append(_post_reply(full_repo, number, int(entry["comment_id"]), entry["body"]))
            thread_id = entry.get("resolve")
            if thread_id:
                _resolve_thread_once(str(thread_id))
                resolved.append(str(thread_id))
    else:
        if comment_id is None or body is None:
            raise typer.BadParameter("pass --comment-id + --body, or --batch")
        posted.append(_post_reply(full_repo, number, comment_id, body))
    _emit({"posted": posted, "resolved": resolved})


@app.command("resolve-thread")
def cmd_resolve_thread(
    thread_id: Annotated[str, typer.Argument(help="GraphQL node ID, e.g. PRRT_xxx")],
) -> None:
    """Resolve a review thread via GraphQL."""
    mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}"
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={mutation}", "-F", f"id={thread_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    thread = payload["data"]["resolveReviewThread"]["thread"]
    _emit({"thread_id": thread["id"], "state": "resolved" if thread["isResolved"] else "open"})


@app.command("post-review")
def cmd_post_review(
    payload_path: Annotated[Path, typer.Argument(help="path to review-payload.json")],
    repo: REPO_OPT = None,
    pr: PR_OPT = None,
) -> None:
    """Validate and POST a review payload to the PR. Prints {id,url} JSON."""
    raw = payload_path.read_text()
    payload = json.loads(raw)
    diff = _gh_pr_diff(repo, pr)
    offenders = _validate_payload(payload, _parse_unified_diff(diff))
    if offenders:
        _emit({"offenders": offenders})
        raise typer.Exit(code=2)

    full_repo, number = _resolve_pr(repo, pr)
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{full_repo}/pulls/{number}/reviews",
            "--method",
            "POST",
            "--input",
            str(payload_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    response = json.loads(result.stdout)
    _emit({"id": response["id"], "url": response.get("html_url", "")})


_DEFAULT_REPOS = os.getenv("PRCTL_DEFAULT_REPOS", "")


def _caller_login() -> str:
    return _gh("user")["login"]


def _check_conclusions(rollup: list[dict[str, Any]]) -> list[str]:
    """Flatten gh's statusCheckRollup to a list of conclusion strings."""
    out: list[str] = []
    for entry in rollup or []:
        # CheckRun uses .conclusion; StatusContext uses .state
        conclusion = entry.get("conclusion") or entry.get("state")
        if conclusion:
            out.append(str(conclusion).upper())
    return out


def _last_commit_date(repo: str, number: int) -> str | None:
    commits = _gh(f"repos/{repo}/pulls/{number}/commits")
    if not commits:
        return None
    return commits[-1].get("commit", {}).get("committer", {}).get("date")


def _last_caller_feedback(repo: str, number: int, caller: str) -> str | None:
    reviews = _gh(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
    comments = _gh(f"repos/{repo}/pulls/{number}/comments", paginate=True)
    stamps: list[str] = []
    for r in reviews:
        if (r.get("user") or {}).get("login") == caller and r.get("submitted_at"):
            stamps.append(r["submitted_at"])
    for c in comments:
        if (c.get("user") or {}).get("login") == caller and c.get("created_at"):
            stamps.append(c["created_at"])
    return max(stamps) if stamps else None


def _pr_summary(pr: dict[str, Any], repo: str, checks: list[str]) -> dict[str, Any]:
    return {
        "repo": repo,
        "number": pr["number"],
        "title": pr.get("title", ""),
        "author": (pr.get("author") or {}).get("login", ""),
        "head": pr.get("headRefName", ""),
        "review_decision": pr.get("reviewDecision", ""),
        "mergeable": pr.get("mergeable", ""),
        "checks": {
            "pass": [c for c in checks if c in _PASS_CONCLUSIONS],
            "fail": [c for c in checks if c in _FAIL_CONCLUSIONS],
            "pending": [c for c in checks if c not in _PASS_CONCLUSIONS | _FAIL_CONCLUSIONS],
        },
    }


@app.command("queue")
def cmd_queue(
    repos: Annotated[
        str,
        typer.Option(
            "--repos",
            help="comma-separated owner/name list; defaults to $PRCTL_DEFAULT_REPOS",
        ),
    ] = _DEFAULT_REPOS,
    with_notes: Annotated[
        bool,
        typer.Option(
            "--with-notes",
            help="Attach a condensed local note (intent, open threads, last session) to each PR entry.",
        ),
    ] = False,
) -> None:
    """Categorize open PRs (not authored by the caller) into review buckets."""
    if not repos:
        raise typer.BadParameter("pass --repos or set PRCTL_DEFAULT_REPOS")
    caller = _caller_login()
    buckets: dict[str, list[dict[str, Any]]] = {
        "ready_merge": [],
        "initial_review": [],
        "another_round": [],
        "awaiting_author": [],
        "drafts": [],
    }
    flags: dict[str, list[str]] = {}

    for repo in [r.strip() for r in repos.split(",") if r.strip()]:
        argv = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,author,isDraft,reviewDecision,mergeable,headRefName,statusCheckRollup",
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
        prs = json.loads(result.stdout)

        for pr in prs:
            if (pr.get("author") or {}).get("login") == caller:
                continue
            checks = _check_conclusions(pr.get("statusCheckRollup") or [])
            pr_with_checks = {**pr, "statusCheckRollup_conclusions": checks}
            summary = _pr_summary(pr, repo, checks)

            key = f"{repo}#{pr['number']}"
            if pr.get("mergeable") == "CONFLICTING":
                flags.setdefault(key, []).append("merge_conflict")
            if any(c in _FAIL_CONCLUSIONS for c in checks):
                flags.setdefault(key, []).append("ci_failing")
            if pr.get("isDraft"):
                flags.setdefault(key, []).append("draft")
                buckets["drafts"].append(summary)
                continue

            last_commit = _last_commit_date(repo, pr["number"])
            last_feedback = _last_caller_feedback(repo, pr["number"], caller)
            owner, name = repo.split("/", 1)
            unresolved = _unresolved_caller_threads(owner, name, pr["number"], caller)
            bucket = categorize_pr(
                pr_with_checks,
                last_commit=last_commit,
                last_caller_feedback=last_feedback,
                unresolved_caller_threads=unresolved,
            )
            summary["last_commit"] = last_commit
            summary["last_caller_feedback"] = last_feedback
            summary["unresolved_caller_threads"] = unresolved
            buckets[bucket].append(summary)

    if with_notes:
        for entries in buckets.values():
            for entry in entries:
                note = notes.try_load_note(entry["repo"], entry["number"])
                entry["notes"] = notes.compact_for_queue(note)

    _emit({**buckets, "flags": flags})


def _pr_view(repo: str, number: int, fields: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "pr", "view", "--repo", repo, str(number), "--json", ",".join(fields)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _pr_by_head(repo: str, head: str) -> dict[str, Any] | None:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--head",
            head,
            "--json",
            "number,headRefName,baseRefName",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    items = json.loads(result.stdout)
    return items[0] if items else None


def _worktree_path_for_branch(branch: str) -> str | None:
    """Return the path of a worktree checked out to ``branch``, or None."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    current_path: str | None = None
    target = f"branch refs/heads/{branch}"
    for raw in result.stdout.splitlines():
        if raw.startswith("worktree "):
            current_path = raw[len("worktree ") :].strip()
        elif raw == target:
            return current_path
    return None


@app.command("stack")
def cmd_stack(
    repo: REPO_OPT = None,
    seed: Annotated[str | None, typer.Option("--seed", help="PR number, branch, or 'current'")] = None,
) -> None:
    """Discover ordered stack from a seed (bottom to top)."""
    full_repo = repo
    if full_repo is None:
        view = _gh_pr_view(["baseRepository", "headRepository", "headRepositoryOwner"])
        base = view.get("baseRepository") or {}
        owner = (view.get("headRepositoryOwner") or base.get("owner") or {}).get("login")
        name = (view.get("headRepository") or base).get("name")
        if owner is None or name is None:
            raise typer.BadParameter("could not determine repo; pass --repo")
        full_repo = f"{owner}/{name}"

    if seed is None or seed == "current":
        view = _gh_pr_view(["number"])
        seed_number = int(view["number"])
        pr = _pr_view(full_repo, seed_number, ["number", "headRefName", "baseRefName"])
    else:
        try:
            seed_number = int(seed)
            pr = _pr_view(full_repo, seed_number, ["number", "headRefName", "baseRefName"])
        except ValueError:
            found = _pr_by_head(full_repo, seed)
            if found is None:
                raise typer.BadParameter(f"no open PR with head {seed!r}") from None
            pr = found

    chain: list[dict[str, Any]] = [pr]
    while chain[0]["baseRefName"] != "main":
        parent = _pr_by_head(full_repo, chain[0]["baseRefName"])
        if parent is None:
            break
        chain.insert(0, parent)

    _emit(
        [
            {
                "number": p["number"],
                "head": p["headRefName"],
                "base": p["baseRefName"],
                "worktree": _worktree_path_for_branch(p["headRefName"]),
            }
            for p in chain
        ]
    )


def _poll_checks(repo: str, pr_number: int) -> list[str]:
    """Return a deduped list of status-check conclusions (upper-cased)."""
    result = subprocess.run(
        ["gh", "pr", "view", "--repo", repo, str(pr_number), "--json", "statusCheckRollup"],
        capture_output=True,
        text=True,
        check=True,
    )
    rollup = json.loads(result.stdout).get("statusCheckRollup", []) or []
    out: list[str] = []
    for entry in rollup:
        # pending checks carry a ``status`` (IN_PROGRESS / QUEUED / PENDING) and no conclusion yet
        value = entry.get("conclusion") or entry.get("status") or entry.get("state")
        if value:
            out.append(str(value).upper())
    return out


@app.command("ci-wait")
def cmd_ci_wait(
    pr_number: Annotated[int, typer.Argument()],
    repo: REPO_OPT = None,
    timeout: Annotated[int, typer.Option("--timeout", help="seconds")] = 600,
    interval: Annotated[int, typer.Option("--interval", help="seconds between polls")] = 30,
) -> None:
    """Poll CI for a PR. Exit 0 green, 1 failing, 2 timeout."""
    full_repo = repo or _resolve_pr(None, None)[0]
    deadline = time.monotonic() + timeout
    while True:
        checks = _poll_checks(full_repo, pr_number)
        fail = [c for c in checks if c in _FAIL_CONCLUSIONS]
        pending = [c for c in checks if c not in _PASS_CONCLUSIONS | _FAIL_CONCLUSIONS]
        if fail:
            _emit(
                {
                    "state": "failing",
                    "checks": {
                        "pass": [c for c in checks if c in _PASS_CONCLUSIONS],
                        "fail": fail,
                        "pending": pending,
                    },
                }
            )
            raise typer.Exit(code=1)
        if not pending:
            _emit(
                {
                    "state": "green",
                    "checks": {
                        "pass": [c for c in checks if c in _PASS_CONCLUSIONS],
                        "fail": [],
                        "pending": [],
                    },
                }
            )
            return
        if time.monotonic() >= deadline:
            _emit(
                {
                    "state": "timeout",
                    "checks": {
                        "pass": [c for c in checks if c in _PASS_CONCLUSIONS],
                        "fail": [],
                        "pending": pending,
                    },
                }
            )
            raise typer.Exit(code=2)
        time.sleep(interval)


@app.command("safe-merge")
def cmd_safe_merge(
    pr_number: Annotated[int, typer.Argument()],
    repo: REPO_OPT = None,
) -> None:
    """Squash-merge a PR; refuse to delete-branch if any open PRs target it."""
    full_repo = repo or _resolve_pr(None, None)[0]

    # 1) head ref
    view = subprocess.run(
        ["gh", "pr", "view", "--repo", full_repo, str(pr_number), "--json", "number,headRefName"],
        capture_output=True,
        text=True,
        check=True,
    )
    head = json.loads(view.stdout)["headRefName"]

    # 2) children — open PRs targeting this branch
    children_raw = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            full_repo,
            "--state",
            "open",
            "--base",
            head,
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    children = [int(c["number"]) for c in json.loads(children_raw.stdout)]

    # 3) merge
    argv = ["gh", "pr", "merge", "--repo", full_repo, str(pr_number), "--squash"]
    if not children:
        argv.append("--delete-branch")
    subprocess.run(argv, capture_output=True, text=True, check=True)

    # 4) merge SHA
    sha_raw = subprocess.run(
        ["gh", "pr", "view", "--repo", full_repo, str(pr_number), "--json", "mergeCommit"],
        capture_output=True,
        text=True,
        check=True,
    )
    merge_sha = (json.loads(sha_raw.stdout).get("mergeCommit") or {}).get("oid", "")

    _emit(
        {
            "merge_sha": merge_sha,
            "deleted_branch": not children,
            "children_blocked": children,
        }
    )


@app.command("rebase-onto-main")
def cmd_rebase_onto_main(
    old_base: Annotated[
        str | None,
        typer.Option(
            "--old-base",
            help="SHA or ref of the previous base; when set, uses `git rebase --onto origin/main <old-base>`",
        ),
    ] = None,
) -> None:
    """Rebase current branch onto origin/main, optionally stripping stale-parent commits."""
    subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True, check=True)
    if old_base is None:
        subprocess.run(["git", "rebase", "origin/main"], capture_output=True, text=True, check=True)
        strategy = "plain"
    else:
        subprocess.run(
            ["git", "rebase", "--onto", "origin/main", old_base],
            capture_output=True,
            text=True,
            check=True,
        )
        strategy = "onto"
    head = _git("rev-parse", "HEAD")
    _emit({"strategy": strategy, "old_base": old_base, "head_after": head})


@app.command("diff-lines")
def cmd_diff_lines(repo: REPO_OPT = None, pr: PR_OPT = None) -> None:
    """Emit {path: [head_line_numbers]} of added/modified lines in the PR diff."""
    if repo is None and pr is None:
        # current branch — still resolve so downstream callers know the PR
        _resolve_pr(repo, pr)
    diff = _gh_pr_diff(repo, pr)
    _emit(_parse_unified_diff(diff))


@app.command("comments")
def cmd_comments(repo: REPO_OPT = None, pr: PR_OPT = None) -> None:
    """Fetch normalized review + inline comments for a PR."""
    full_repo, number = _resolve_pr(repo, pr)
    owner, name = full_repo.split("/", 1)

    reviews = _gh(f"repos/{full_repo}/pulls/{number}/reviews", paginate=True)
    inline = _gh(f"repos/{full_repo}/pulls/{number}/comments", paginate=True)
    threads = _review_threads(owner, name, number)

    out: list[dict[str, Any]] = []
    for c in inline:
        meta = threads.get(c["id"], {})
        out.append(
            {
                "id": c["id"],
                "thread_id": meta.get("thread_id"),
                "kind": "inline",
                "path": c.get("path"),
                "line": c.get("line"),
                "body": c.get("body", ""),
                "author": (c.get("user") or {}).get("login"),
                "created_at": c.get("created_at"),
                "reply_url": f"repos/{full_repo}/pulls/{number}/comments/{c['id']}/replies",
                "is_resolved": meta.get("is_resolved", False),
            }
        )
    for r in reviews:
        body = (r.get("body") or "").strip()
        if not body:
            continue
        out.append(
            {
                "id": r["id"],
                "thread_id": None,
                "kind": "review_body",
                "path": None,
                "line": None,
                "body": body,
                "author": (r.get("user") or {}).get("login"),
                "created_at": r.get("submitted_at"),
                "reply_url": None,
                "is_resolved": False,
            }
        )

    _emit(out)


# ---------- notes subsystem ----------

_NOTES_REPO_OPT = Annotated[str, typer.Option("--repo", help="owner/name")]
_NOTES_PR_OPT = Annotated[int, typer.Option("--pr", help="PR number")]


def _read_or_stdin(spec: str) -> str:
    """``-`` → stdin, ``@path`` → file, anything else → literal string."""
    if spec == "-":
        return sys.stdin.read()
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


@notes_app.command("get")
def cmd_notes_get(repo: _NOTES_REPO_OPT, pr: _NOTES_PR_OPT) -> None:
    """Print the note for a PR (empty-note skeleton if no file exists)."""
    _emit(notes.get_note(repo, pr))


@notes_app.command("set")
def cmd_notes_set(
    repo: _NOTES_REPO_OPT,
    pr: _NOTES_PR_OPT,
    summary_sha: Annotated[str, typer.Option("--summary-sha", help="head SHA the intent was derived from")],
    summary_intent: Annotated[
        str, typer.Option("--summary-intent", help="one-paragraph description of what the PR does")
    ],
    summary_scope: Annotated[
        list[str] | None,
        typer.Option("--summary-scope", help="repeat for each scope item"),
    ] = None,
) -> None:
    """Set or replace the PR summary block."""
    _emit(notes.set_summary(repo, pr, sha=summary_sha, intent=summary_intent, scope=summary_scope))


@notes_app.command("append")
def cmd_notes_append(
    repo: _NOTES_REPO_OPT,
    pr: _NOTES_PR_OPT,
    session: Annotated[
        str,
        typer.Option("--session", help="session JSON; '-' reads stdin, '@path' reads file"),
    ],
) -> None:
    """Append a review session record."""
    data = json.loads(_read_or_stdin(session))
    _emit(notes.append_session(repo, pr, data))


@notes_app.command("track-thread")
def cmd_notes_track_thread(
    repo: _NOTES_REPO_OPT,
    pr: _NOTES_PR_OPT,
    thread_id: Annotated[str, typer.Option("--thread-id")],
    note_text: Annotated[str, typer.Option("--note", help="what addressing this thread should look like")],
    sha: Annotated[str, typer.Option("--sha", help="head SHA at time of record")],
) -> None:
    """Track a review thread we're waiting on the author to address."""
    _emit(notes.track_thread(repo, pr, thread_id=thread_id, note_text=note_text, sha=sha))


@notes_app.command("untrack-thread")
def cmd_notes_untrack_thread(
    repo: _NOTES_REPO_OPT,
    pr: _NOTES_PR_OPT,
    thread_id: Annotated[str, typer.Option("--thread-id")],
) -> None:
    """Stop tracking a thread (e.g. after it's been addressed)."""
    _emit(notes.untrack_thread(repo, pr, thread_id=thread_id))


@notes_app.command("list")
def cmd_notes_list(
    repo: Annotated[str | None, typer.Option("--repo", help="filter by owner/name")] = None,
) -> None:
    """List PR notes on disk (optionally filtered to one repo)."""
    _emit(notes.list_notes(repo))


@notes_app.command("path")
def cmd_notes_path(repo: _NOTES_REPO_OPT, pr: _NOTES_PR_OPT) -> None:
    """Print the absolute path to a PR's note file (not JSON — plain string for piping)."""
    typer.echo(str(notes.note_path(repo, pr)))


@notes_app.command("export")
def cmd_notes_export(
    output_dir: Annotated[Path, typer.Argument(help="directory to receive a copy of all local note files")],
) -> None:
    """Export all local note files to ``output_dir`` for manual backup/sharing."""
    root = notes.notes_root()
    if output_dir.exists():
        shutil.rmtree(root)
    os.system(f"cp -R {root} {output_dir}")
    _emit({"exported_from": str(root), "output_dir": str(output_dir)})


@notes_app.command("migrate")
def cmd_notes_migrate() -> None:
    """Placeholder for future schema migrations. No-op at the current version."""
    _emit({"schema_version": notes.SCHEMA_VERSION, "migrated": 0})


if __name__ == "__main__":
    app()
