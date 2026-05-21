# ITU Downloader

Archive ITU-T test signals and publications from the [ITU website](https://www.itu.int/rec/T-REC-P/en) for research and offline access.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Usage

```bash
# Recommended: download codec publications + test signals
just run

# Dry run first to see what would be downloaded
just run --dry-run

# Show all options
just run --help
```

`just run` with no arguments downloads publications listed in [`codec-recommendations.txt`](codec-recommendations.txt) plus ITU-T test signal vectors. Edit that file to add or remove recommendations.

### Common overrides

```bash
# Publications only, no test signals
just run --include-publications --allow-list codec-recommendations.txt

# Specific recommendations only
just run --include-publications --allow-list P.862,P.863

# Everything (broad series crawl, all editions)
just run --include-publications --download-all

# Custom output directory
just run --out /data/itu-archive
```

> Without `just`, use `uv run --with httpx --with beautifulsoup4 --with playwright --with rich python itu_downloader.py <args>` directly.

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Lint, format, type-check, test
just lint
just format
just typecheck
just test

# Or run all checks at once
just ci
```

## License

MIT - see [LICENSE](LICENSE).
