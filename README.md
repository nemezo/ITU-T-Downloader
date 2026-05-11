# ITU Downloader

Archive ITU-T test signals and publications with a deterministic folder layout.

The script uses Playwright for JavaScript-rendered ITU pages, stores every downloaded artifact in a stable hierarchy, and appends a JSONL manifest with SHA-256 checksums for auditability.

## Features

- Discover and download ITU-T test signal vector packages
- Discover codec-focused ITU-T Recommendation pages by profile or allow-list
- Download PDFs, archives, source-code attachments, and test-vector payloads
- Deterministic output path: `collection/series/recommendation/edition/type/`
- Resumable downloads with byte-range requests
- JSONL manifest with SHA-256 checksums per artifact
- Graceful shutdown on SIGINT/SIGTERM

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

## Output layout

```text
research/itu-archive/
├── index/
│   ├── discovered-pages.json
│   ├── accepted-pages.json
│   └── rejected-pages.json
├── manifest.jsonl
└── <collection>/<series>/<recommendation>/<edition>/<type>/
    └── <artifact>
```

## License

MIT - see [LICENSE](LICENSE).
