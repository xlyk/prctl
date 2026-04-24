# prctl

PR-workflow helper CLI. Thin shell over `gh` and `git` that handles the mechanical parts of review / merge / stack workflows so agents and humans can focus on judgment.

## Install

```bash
uv tool install git+https://github.com/xlyk/prctl.git
# or, from a clone
uv tool install --from . prctl
```

Requires `gh` and `git` on PATH. Python 3.12+.

## Subcommands

```bash
prctl comments [--repo o/r] [--pr N]           # normalized review + inline comments
prctl diff-lines [--repo o/r] [--pr N]         # {path: [head_line_numbers]} for added/modified lines
prctl validate-review <payload.json>           # check (path,line) against diff; exit 2 on bad lines
prctl post-review <payload.json>               # validate then POST; prints {id,url}
prctl reply --comment-id N --body "..."        # single inline reply
prctl reply --batch replies.json               # batch inline replies
prctl resolve-thread PRRT_xxx                  # GraphQL resolveReviewThread
prctl queue [--repos o/r1,o/r2]                # categorize open PRs into review buckets
prctl stack [--repo o/r] [--seed <pr|branch>]  # bottom-to-top stack order
prctl safe-merge <pr> [--repo o/r]             # squash-merge, children-aware --delete-branch
prctl rebase-onto-main [--old-base <sha>]      # rebase onto origin/main, optionally --onto
```

All subcommands emit JSON to stdout unless noted.

Exit codes: `0` ok, `1` generic error, `2` validation failure.

## Config

- `PRCTL_DEFAULT_REPOS` — comma-separated `owner/name` list used by `prctl queue` when `--repos` is omitted.

## Claude Code skill

`skills/prctl/SKILL.md` teaches agents how to call the CLI correctly — output contracts, exit codes, and gotchas (HEAD-side line numbers, pagination, children-aware safe-merge). Install by symlinking into your user skills dir:

```bash
just install-skill
# or manually:
ln -snf "$(pwd)/skills/prctl" ~/.claude/skills/prctl
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
uv run ty check src
```
