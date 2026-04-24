default:
    @just --list

test:
    uv run pytest tests/ -q

lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

check: lint test
    uv run ty check src

install:
    uv tool install --from . prctl --reinstall

install-skill:
    mkdir -p $HOME/.claude/skills
    ln -snf {{justfile_directory()}}/skills/prctl $HOME/.claude/skills/prctl
    @echo "installed: $HOME/.claude/skills/prctl -> {{justfile_directory()}}/skills/prctl"
