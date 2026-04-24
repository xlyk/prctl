---
name: prctl
description: Use when invoking the `prctl` CLI to operate on GitHub PRs — fetching review comments, validating/posting reviews, replying/resolving threads, categorizing the PR queue, walking stacked PRs, children-aware squash-merge, or rebase-onto-main. Loads JSON output contracts, exit-code conventions, and gotchas so calls land correctly on the first try.
---

# prctl — PR-workflow helper

`prctl` is a CLI over `gh` and `git`. Every subcommand emits JSON on stdout. Exit codes: `0` ok, `1` generic error, `2` validation failure.

## When to reach for it

- `prctl comments` — normalized review + inline comments (paginated, with thread IDs)
- `prctl diff-lines` — `{path: [head_line_numbers]}` of added/modified lines
- `prctl validate-review <payload.json>` — pre-flight check for a review payload's `(path, line)` pairs
- `prctl post-review <payload.json>` — validate then POST; prints `{id, url}`
- `prctl reply --comment-id N --body "..."` or `--batch file.json`
- `prctl resolve-thread <thread_id>`
- `prctl queue [--repos o/r1,o/r2]` — categorize open PRs into review buckets
- `prctl stack [--repo o/r] [--seed <pr|branch>]` — bottom-to-top stack chain
- `prctl safe-merge <pr>` — children-aware squash-merge
- `prctl rebase-onto-main [--old-base <sha>]`
- `prctl ci-wait <pr> [--timeout 600] [--interval 30]` — poll `statusCheckRollup` until green / failing / timeout

## When NOT to reach for it

- Drafting review comment text, reply wording, or PR bodies — that is agent judgment
- Creating draft PRs — use `gh pr create --draft` directly
- Reading random repo metadata — use `gh` / `git` directly

## Output contracts

### `comments` → `list`

```json
[
  {
    "id": 111,
    "thread_id": "PRRT_xxx" | null,
    "kind": "inline" | "review_body",
    "path": "app/x.py" | null,
    "line": 42 | null,
    "body": "...",
    "author": "login",
    "created_at": "2026-04-23T...",
    "reply_url": "repos/o/r/pulls/N/comments/111/replies" | null,
    "is_resolved": false
  }
]
```

Empty review bodies are dropped. `thread_id` and `is_resolved` come from the GraphQL `reviewThreads` query — use `thread_id` for `resolve-thread`.

### `diff-lines` → `dict`

`{"app/x.py": [12, 13, 42], "tests/test_x.py": [5, 6]}` — HEAD-side line numbers of added lines. Use these as the `line` / `start_line` in review-payload comments.

### `validate-review` → exit 0 silent OR exit 2 + offenders

```json
{"offenders": [{"path": "a.py", "line": 99, "reason": "not an added/modified line in the PR diff"}]}
```

### `post-review` → `{id, url}`

Review payload shape required:

```json
{
  "commit_id": "<head sha>",
  "event": "COMMENT",
  "body": "",
  "comments": [
    {"path": "app/x.py", "line": 42, "side": "RIGHT", "body": "..."}
  ]
}
```

Multi-line: include `start_line` + `start_side: "RIGHT"`. Keep top-level `body` empty unless there's a cross-cutting architectural point.

### `reply` → `{"posted": [id, ...], "resolved": [thread_id, ...]}`

Batch file shape: `[{"comment_id": 123, "body": "...", "resolve": "PRRT_xxx"?}, ...]`. `resolve` is optional per entry — when present, the thread is resolved immediately after the reply lands. `resolved` in the output lists the thread IDs that were resolved.

### `resolve-thread` → `{"thread_id", "state": "resolved" | "open"}`

### `queue` → buckets + flags

```json
{
  "ready_merge": [...],
  "initial_review": [...],
  "another_round": [...],
  "awaiting_author": [...],
  "drafts": [...],
  "flags": {"<repo>#<number>": ["merge_conflict" | "ci_failing" | "draft"]}
}
```

Each entry: `{repo, number, title, author, head, review_decision, mergeable, checks: {pass, fail, pending}, last_commit, last_caller_feedback}`.

Bucketing rules:
- `ready_merge`: `reviewDecision == "APPROVED"` AND `mergeable == "MERGEABLE"` AND no failing checks.
- `initial_review`: caller has never reviewed/commented.
- `another_round`: caller reviewed, but last commit is newer than caller's last feedback.
- `awaiting_author`: caller's feedback is newer than last commit.

### `stack` → `list`

`[{number, head, base, worktree: "/path" | null}, ...]` bottom-to-top. `worktree` resolves via `git worktree list --porcelain` when one is checked out.

### `safe-merge` → `{merge_sha, deleted_branch, children_blocked}`

### `rebase-onto-main` → `{strategy: "plain" | "onto", old_base, head_after}`

### `ci-wait` → `{state: "green" | "failing" | "timeout", checks: {pass, fail, pending}}`

Exit code mirrors `state`: 0 green, 1 failing, 2 timeout. Defaults: `--timeout 600` (10m), `--interval 30` seconds. Use inside a merge flow to gate on CI without agent-side polling loops.

## Gotchas

- **HEAD-side line numbers.** Every `line` / `start_line` in a review payload MUST be an absolute line number at the PR's HEAD, not a diff-hunk position. Use `prctl diff-lines` to enumerate valid lines before drafting — GitHub rejects out-of-diff comments.
- **Validate before posting.** Run `prctl validate-review payload.json` first; act on any `offenders`. `post-review` re-validates and exits 2 on failure, but catching locally saves a round trip.
- **`queue --repos`.** Pass explicit `--repos owner/name[,owner/name...]` OR export `PRCTL_DEFAULT_REPOS`. Default is empty; `queue` errors with `BadParameter` if both are missing.
- **`safe-merge` + stacks.** The CLI withholds `--delete-branch` when any open PR targets the merging branch — this prevents GitHub auto-closing downstream PRs.
- **`reply --body` preserves Unicode.** Body goes through `gh api -f body=...`; em-dashes, fancy quotes, and non-ASCII survive.
- **Pagination is automatic.** `comments` and `queue` use `gh --paginate`, so multi-page reviews/comments are not lost.
- **JSON only.** No human-mode rendering yet. Parse with `jq` or `json.loads`.

## Common patterns

### Draft + post a review

1. `prctl diff-lines --repo o/r --pr N > lines.json` — know which lines are commentable.
2. Build `payload.json` using only lines present in the map.
3. `prctl validate-review payload.json --repo o/r --pr N` — fix any offenders.
4. After human approval, `prctl post-review payload.json --repo o/r --pr N`.

### Address review comments

1. `prctl comments --repo o/r --pr N > comments.json` — triage against current file state at HEAD.
2. Make fixes, commit, push.
3. Build `replies.json` — one entry per approved reply; add `"resolve": "<thread_id>"` to entries whose thread should be resolved:
   ```json
   [{"comment_id": 123, "body": "fixed — ...", "resolve": "PRRT_abc"},
    {"comment_id": 456, "body": "declined — out of scope"}]
   ```
4. `prctl reply --repo o/r --pr N --batch replies.json` — posts replies and resolves threads atomically in one pass.

### Walk a stack

1. `prctl stack --repo o/r --seed 123` — get bottom-to-top order.
2. For each PR: switch branch, `prctl rebase-onto-main` (optionally with `--old-base` if parent was just merged), `prctl ci-wait <pr>`, then (after a merge gate) `prctl safe-merge <pr>`.

## Mutating subcommands do NOT gate

These subcommands make visible, hard-to-undo changes to GitHub. They do not prompt, do not preview, and do not confirm. The caller is responsible for gating every one of them behind an AskUserQuestion (or equivalent) approval, and for previewing the content verbatim in chat before asking.

- `prctl post-review` — posts a review with inline comments. Every `(path, line, body)` must be approved first.
- `prctl reply` / `prctl reply --batch` — posts inline replies. Every reply body must be approved first. If the batch file contains entries the user did not approve, they WILL be posted — construct the batch file from the approved set only.
- `prctl resolve-thread` — resolves a GitHub review thread. Only call for comments the user confirmed are actually fixed.
- `prctl safe-merge` — squash-merges and may delete the branch. Call only after an explicit merge gate.
- `prctl rebase-onto-main` — rewrites branch history and will need a `--force-with-lease` push afterwards. Don't run on someone else's branch or mid-review.

Read-only subcommands (`comments`, `diff-lines`, `queue`, `stack`, `validate-review`, `ci-wait`) do not need approval.

## Not handled by prctl

- AskUserQuestion gates — the calling workflow owns approval.
- Verbatim comment previews with current-HEAD code snippets — `prctl comments` returns the body but NOT the source at that line. Agent must `Read` the file to show the current code.
- Force-with-lease push, pytest, pre-commit — run directly.
- Writing review/reply/PR prose — agent judgment.
