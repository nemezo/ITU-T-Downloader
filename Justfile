set dotenv-load

# Default: list available recipes
default:
    @just --list

# Run ruff linter
lint:
    uv run ruff check .

# Run ruff formatter (check mode)
format-check:
    uv run ruff format --check .

# Apply ruff formatter
format:
    uv run ruff format .

# Run pyright type checker
typecheck:
    uv run --with pyright pyright itu_downloader.py

# Run all unit tests
test:
    uv run python -m pytest tests/ -v

# Run all CI checks (lint + format-check + typecheck + test)
ci: lint format-check typecheck test

# Run the downloader.
# With no args: downloads publications from codec-recommendations.txt + test signals.
# Pass any flags to override, e.g.:
#   just run --dry-run
#   just run --include-publications --allow-list codec-recommendations.txt
#   just run --include-publications --download-all
run *args:
    uv run --with httpx --with beautifulsoup4 --with playwright --with rich \
        python itu_downloader.py \
        {{ if args == "" { "--include-publications --include-test-signals --allow-list codec-recommendations.txt" } else { args } }}
