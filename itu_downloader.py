#!/usr/bin/env python3
"""
Archive ITU-T test signals and publications with a deterministic folder layout.

The script uses Playwright because some ITU pages are JavaScript-rendered.
It stores every downloaded artifact in a stable hierarchy and appends a JSONL
manifest with SHA-256 checksums for auditability.

Main capabilities:
  - Discover ITU-T test signal vector pages
  - Discover codec-focused ITU-T Recommendation pages by profile or allow-list
  - Download PDFs, archives, source-code attachments, and test-vector payloads
  - Sort by collection, series, recommendation, edition date, and artifact type
  - Resume previous runs using manifest.jsonl

Example:
  uv run --with httpx --with beautifulsoup4 --with playwright --with rich \\
    python itu_archive_downloader.py \\
    --out itu-archive \\
    --profile codecs \\
    --include-test-signals \\
    --include-publications \\
    --delay 1.5 \\
    --verbose
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import signal
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

TEST_SIGNALS_URL = "https://www.itu.int/myworkspace/t-signals"
RECOMMENDATION_INDEX_URL = "https://www.itu.int/itu-t/recommendations/index.aspx?ser={series}"
MWS_API_URL = "https://www.itu.int/mws"
TEST_SIGNALS_API_URL = f"{MWS_API_URL}/api/testsignals/allsignals"
TEST_SIGNAL_FILES_API_URL = f"{MWS_API_URL}/api/testsignals/signalfiles"
RECOMMENDATIONS_SEARCH_API_URL = f"{MWS_API_URL}/api/recommendations/searchRecs"
MAX_SERIES_FOLDER_LENGTH = 48
BROAD_SERIES = ("G", "H", "J", "P", "T", "V")

SERIES_FOLDER_LABELS = {
    "G": "G-transmission-systems-media-networks",
    "H": "H-audiovisual-multimedia-systems",
    "J": "J-cable-tv-sound-multimedia",
    "P": "P-transmission-quality-local-lines",
    "T": "T-telematic-terminals",
    "V": "V-data-over-telephone-network",
}

RECOMMENDATION_CATEGORY_BY_SERIES = {
    "G": "audio-speech",
    "H": "video",
    "J": "quality",
    "P": "quality",
    "T": "image",
}

CODEC_RECOMMENDATIONS = (
    "G.191",
    "G.711",
    "G.711.0",
    "G.711.1",
    "G.718",
    "G.719",
    "G.722",
    "G.722.1",
    "G.722.2",
    "G.723.1",
    "G.726",
    "G.727",
    "G.728",
    "G.729",
    "G.729.1",
    "H.261",
    "H.262",
    "H.263",
    "H.264",
    "H.264.2",
    "H.265",
    "H.265.2",
    "H.266",
    "H.266.2",
    "H-Suppl-21",
    "T.81",
    "T.82",
    "T.800",
    "T.801",
    "T.802",
    "T.803",
    "T.804",
    "T.805",
    "T.807",
    "T.808",
    "T.809",
    "T.810",
    "T.812",
    "T.813",
    "T.814",
    "T.815",
    "T.840.1",
    "T.840.2",
    "T.840.3",
    "T.840.5",
    "P.50",
    "P.56",
    "P.501",
    "P.800",
    "P.800.1",
    "P.800.2",
    "P.808",
    "P.810",
    "P.835",
    "P.862",
    "P.862.1",
    "P.862.2",
    "P.862.3",
    "P.863",
    "P.863.1",
    "P.863.2",
    "P.1203",
    "P.1203.1",
    "P.1203.2",
    "P.1203.3",
    "P.1204",
    "P.1204.1",
    "P.1204.2",
    "P.1204.3",
    "P.1204.4",
    "P.1204.5",
)
CODEC_RECOMMENDATION_SET = frozenset(CODEC_RECOMMENDATIONS)

DEPENDENCY_COMMAND = (
    "uv run --with httpx --with beautifulsoup4 --with playwright --with rich "
    "python research/itu_archive_downloader.py"
)


class MissingDependencyError(RuntimeError):
    """Raised when an optional runtime dependency is required but unavailable."""


class DownloadInterrupted(RuntimeError):
    """Raised when shutdown was requested while a download was in progress."""


class PlainProgress:
    """No-op progress adapter used when Rich is unavailable."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def add_task(self, *args, **kwargs) -> int:
        return 0

    def update(self, *args, **kwargs) -> None:
        return None

    def advance(self, *args, **kwargs) -> None:
        return None


class ProgressReporter:
    """Optional Rich progress, with plain stdout fallback for verbose runs."""

    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.console: Any = None
        self.progress_class: Any = None
        self.progress_columns: tuple[Any, ...] = ()
        self.table_class: Any = None

        try:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )
            from rich.table import Table
        except ModuleNotFoundError:
            return

        self.console = Console()
        self.progress_class = Progress
        self.progress_columns = (
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        self.table_class = Table

    @property
    def rich_enabled(self) -> bool:
        return self.console is not None and self.progress_class is not None

    def progress(self):
        if not self.rich_enabled:
            return PlainProgress()

        return self.progress_class(
            *self.progress_columns,
            console=self.console,
            transient=False,
        )

    def log(self, message: str, style: str = "") -> None:
        if self.rich_enabled:
            self.console.print(message, style=style)
        elif self.verbose:
            print(message)

    def summary(self, stats: dict[str, int], output_root: Path) -> None:
        if self.rich_enabled and self.table_class is not None:
            table = self.table_class(title="ITU archive run summary")
            table.add_column("Metric", style="cyan")
            table.add_column("Count", justify="right", style="green")

            for key, value in stats.items():
                table.add_row(key.replace("_", " "), str(value))

            table.add_row("output", str(output_root))
            self.console.print(table)
            return

        if self.verbose:
            print("ITU archive run summary")

            for key, value in stats.items():
                print(f"{key.replace('_', ' ')}: {value}")

            print(f"output: {output_root}")


def shutdown_requested(shutdown_event: threading.Event | None) -> bool:
    """Return whether a graceful shutdown has been requested."""

    return shutdown_event is not None and shutdown_event.is_set()


def install_shutdown_handlers(shutdown_event: threading.Event) -> dict[int, Any]:
    """Trap Ctrl+C/SIGTERM and ask workers to stop after current chunks."""

    previous_handlers: dict[int, Any] = {}

    def handle_shutdown(signum, frame) -> None:
        shutdown_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_shutdown)

    return previous_handlers


def restore_shutdown_handlers(previous_handlers: dict[int, Any]) -> None:
    """Restore signal handlers changed by install_shutdown_handlers."""

    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def missing_dependency_message(package: str) -> str:
    """Return a concise install/run hint for optional runtime dependencies."""

    return (
        f"Missing dependency '{package}'. Run with dependencies, for example: "
        f"{DEPENDENCY_COMMAND} --include-test-signals --dry-run"
    )


def require_httpx():
    """Import httpx only when HTTP downloads are requested."""

    try:
        import httpx
    except ModuleNotFoundError as error:
        if error.name == "httpx":
            raise MissingDependencyError(missing_dependency_message("httpx")) from error

        raise

    return httpx


def require_beautiful_soup():
    """Import BeautifulSoup only when HTML parsing is requested."""

    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as error:
        if error.name == "bs4":
            raise MissingDependencyError(missing_dependency_message("beautifulsoup4")) from error

        raise

    return BeautifulSoup


def require_playwright():
    """Import Playwright only when page rendering is requested."""

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as error:
        if error.name == "playwright":
            raise MissingDependencyError(missing_dependency_message("playwright")) from error

        raise

    return sync_playwright, PlaywrightTimeoutError


ALLOWED_DOMAINS = {
    "www.itu.int",
    "itu.int",
    "handle.itu.int",
}

PUBLICATION_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xml",
}

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".7z",
    ".tar",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".rar",
}

AUDIO_EXTENSIONS = {
    ".wav",
    ".raw",
    ".pcm",
    ".au",
    ".aif",
    ".aiff",
    ".flac",
}

DATA_EXTENSIONS = {
    ".bin",
    ".dat",
    ".txt",
    ".csv",
    ".json",
    ".yuv",
    ".rgb",
    ".264",
    ".265",
    ".266",
    ".bit",
    ".bitstream",
}

SOURCE_EXTENSIONS = {
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".f",
    ".for",
    ".m",
    ".py",
    ".java",
}

DOWNLOAD_EXTENSIONS = (
    PUBLICATION_EXTENSIONS
    | ARCHIVE_EXTENSIONS
    | AUDIO_EXTENSIONS
    | DATA_EXTENSIONS
    | SOURCE_EXTENSIONS
)

BINARY_CONTENT_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/vnd.wave",
}

INACTIVE_RECOMMENDATION_STATUS_TERMS = (
    "superseded",
    "withdrawn",
    "obsolete",
    "deleted",
)

RECOMMENDATION_PATTERN = re.compile(
    r"\b("
    r"[A-Z]\.?\s*(?:Suppl\.?\s*)?\d+(?:\.\d+)?"
    r"(?:\s*App\.?\s*[IVXLC]+)?"
    r"|[A-Z]\s*Suppl\.?\s*\d+"
    r")\b",
    re.IGNORECASE,
)

APPROVAL_DATE_PATTERN = re.compile(
    r"\b(?:Approval date|Approved|Date)\s*[:\-]?\s*"
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)

YEAR_MONTH_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})[-/](0?[1-9]|1[0-2])\b")
MONTH_YEAR_PATTERN = re.compile(r"\b(0?[1-9]|1[0-2])/(19\d{2}|20\d{2})\b")


@dataclass(frozen=True)
class PageRecord:
    """A discovered page that may contain downloadable ITU artifacts."""

    collection: str
    url: str
    title: str
    series: str
    recommendation: str
    edition: str
    page_id: str
    edition_group: str = "latest"


@dataclass(frozen=True)
class DownloadRecord:
    """One manifest entry for a downloaded or skipped artifact."""

    collection: str
    page_url: str
    page_title: str
    series: str
    recommendation: str
    edition: str
    artifact_type: str
    source_url: str
    final_url: str
    output_path: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    downloaded_at_utc: str


@dataclass(frozen=True)
class AssetRecord:
    """One unique asset candidate discovered on an ITU page."""

    page: PageRecord
    source_url: str
    link_text: str


def normalize_text(value: str) -> str:
    """Normalize whitespace for stable matching and filenames."""

    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str, fallback: str = "item", max_length: int = 140) -> str:
    """Create a filesystem-safe name without losing useful identifiers."""

    cleaned = re.sub(r"[^A-Za-z0-9._+-]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:max_length] or fallback


def normalize_recommendation(value: str) -> str:
    """Normalize ITU-T Recommendation identifiers for folder names."""

    text = normalize_text(value).upper()
    text = text.replace(" ", "")
    text = text.replace("SUPPL.", "-Suppl-")
    text = text.replace("SUPPL", "-Suppl-")
    text = text.replace("APP.", "-App-")
    text = text.replace("APP", "-App-")
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def parse_recommendation_list(text: str) -> frozenset[str]:
    """Parse allow-list text into normalized Recommendation identifiers."""

    recommendations: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()

        if not line:
            continue

        for value in line.split(","):
            recommendation = normalize_recommendation(value)

            if recommendation:
                recommendations.append(recommendation)

    return frozenset(recommendations)


def load_allow_list(path: str) -> frozenset[str]:
    """Load a custom Recommendation allow-list file."""

    return parse_recommendation_list(Path(path).read_text(encoding="utf-8"))


def find_recommendation(*texts: str) -> str:
    """Extract a Recommendation identifier from page text."""

    joined = " ".join(texts)
    match = RECOMMENDATION_PATTERN.search(joined)

    if not match:
        return "UNKNOWN"

    return normalize_recommendation(match.group(1))


def recommendation_series(recommendation: str) -> str:
    """Return the first letter of an ITU-T Recommendation identifier."""

    match = re.match(r"([A-Z])", recommendation.upper())
    return match.group(1) if match else "UNKNOWN"


def parse_series_values(value: str) -> list[str]:
    """Parse comma-separated ITU-T series letters."""

    return [item.strip().upper() for item in value.split(",") if item.strip()]


def selected_recommendations(args: argparse.Namespace) -> frozenset[str] | None:
    """Return the active Recommendation allow-list, or None for broad mode."""

    if args.download_all:
        return None

    if args.allow_list:
        return load_allow_list(args.allow_list)

    if args.profile == "all":
        return None

    return CODEC_RECOMMENDATION_SET


def series_values_for_publications(
    args: argparse.Namespace,
    allowed_recommendations: frozenset[str] | None,
) -> list[str]:
    """Return publication series to query for the active download policy."""

    explicit_series = parse_series_values(args.series)

    if explicit_series:
        return explicit_series

    if allowed_recommendations is None:
        return list(BROAD_SERIES)

    series_values = {
        recommendation_series(recommendation) for recommendation in allowed_recommendations
    }
    series_values.discard("UNKNOWN")
    return sorted(series_values)


def filter_pages_by_recommendations(
    pages: Iterable[PageRecord],
    allowed_recommendations: frozenset[str] | None,
) -> tuple[list[PageRecord], list[dict[str, str]]]:
    """Split discovered pages into accepted/rejected profile buckets."""

    deduped = dedupe_pages(pages)

    if allowed_recommendations is None:
        return deduped, []

    accepted: list[PageRecord] = []
    rejected: list[dict[str, str]] = []

    for page in deduped:
        recommendation = normalize_recommendation(page.recommendation)

        if recommendation in allowed_recommendations:
            accepted.append(page)
        else:
            record = asdict(page)
            record["reason"] = "not-in-active-recommendation-allow-list"
            rejected.append(record)

    return accepted, rejected


def series_folder_name(series: str) -> str:
    """Return a compact, descriptive folder name for an ITU-T series."""

    clean_series = (series or "UNKNOWN").strip().upper()
    label = SERIES_FOLDER_LABELS.get(clean_series, f"{clean_series}-series")
    return slugify(label, "UNKNOWN-series", MAX_SERIES_FOLDER_LENGTH)


def recommendation_category(page: PageRecord) -> str:
    """Return the top-level codec archive domain for a Recommendation."""

    series = page.series or recommendation_series(page.recommendation)
    return RECOMMENDATION_CATEGORY_BY_SERIES.get(series.upper(), "other")


def extract_val(url: str) -> str:
    """Extract the test-signal vector ID from a URL."""

    query = parse_qs(urlparse(url).query)
    return query.get("val", ["unknown"])[0]


def is_allowed_url(url: str) -> bool:
    """Restrict crawler and downloader to ITU-controlled hosts."""

    host = urlparse(url).netloc.lower()
    return host in ALLOWED_DOMAINS


def is_preferred_language_download(url: str) -> bool:
    """Keep English ITU dologin artifacts and language-neutral payloads."""

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    languages = [value.lower() for value in query.get("lang", [])]

    if languages and not any(language in {"e", "en"} for language in languages):
        return False

    item_id = query.get("id", [""])[0]

    if not item_id:
        return True

    language_match = re.search(r"-([A-Z])(?:\.[A-Za-z0-9]+)?$", item_id.upper())

    if language_match:
        return language_match.group(1) == "E"

    return True


def render_html(url: str, timeout_ms: int) -> str:
    """Render a potentially JavaScript-heavy page and return HTML."""

    sync_playwright, playwright_timeout_error = require_playwright()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except playwright_timeout_error:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            html = page.content()
        finally:
            browser.close()

    return html


def parse_html_links(base_url: str, html: str) -> list[tuple[str, str]]:
    """Return normalized links and their visible text."""

    BeautifulSoup = require_beautiful_soup()
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []

    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"])
        text = normalize_text(anchor.get_text(" ", strip=True))

        if not text:
            labels = [
                str(anchor.get("title") or ""),
                str(anchor.get("aria-label") or ""),
            ]
            labels.extend(str(image.get("alt") or "") for image in anchor.find_all("img"))
            text = normalize_text(" ".join(labels))

        if is_allowed_url(url):
            links.append((url, text))

    return links


def page_title_from_html(html: str, fallback: str) -> str:
    """Extract a stable page title from HTML."""

    BeautifulSoup = require_beautiful_soup()
    soup = BeautifulSoup(html, "html.parser")

    if soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))

        if title:
            return title

    heading = soup.find(["h1", "h2", "h3"])

    if heading:
        title = normalize_text(heading.get_text(" ", strip=True))

        if title:
            return title

    return fallback


def page_text_from_html(html: str) -> str:
    """Extract visible text from HTML."""

    BeautifulSoup = require_beautiful_soup()
    soup = BeautifulSoup(html, "html.parser")
    return normalize_text(soup.get_text(" ", strip=True))


def infer_edition(text: str, url: str) -> str:
    """Infer a year-month or date folder from page content or URL."""

    date_match = APPROVAL_DATE_PATTERN.search(text)

    if date_match:
        parsed = parse_date_to_year_month(date_match.group(1))

        if parsed:
            return parsed

    year_month_match = YEAR_MONTH_PATTERN.search(url)

    if year_month_match:
        year = year_month_match.group(1)
        month = int(year_month_match.group(2))
        return f"{year}-{month:02d}"

    return "unknown-edition"


def parse_date_to_year_month(value: str) -> str | None:
    """Parse common ITU date strings to YYYY-MM."""

    month_year_match = MONTH_YEAR_PATTERN.search(value)

    if month_year_match:
        month = int(month_year_match.group(1))
        year = int(month_year_match.group(2))
        return f"{year:04d}-{month:02d}"

    candidates = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d %B %Y",
        "%B %Y",
    ]

    for candidate in candidates:
        try:
            parsed = datetime.strptime(value, candidate)
            return f"{parsed.year:04d}-{parsed.month:02d}"
        except ValueError:
            continue

    return None


def is_download_candidate(url: str, link_text: str) -> bool:
    """Identify likely downloadable artifacts without leaving ITU domains."""

    if not is_preferred_language_download(url):
        return False

    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix.lower()
    lower_text = link_text.lower()
    lower_url = url.lower()

    if suffix in DOWNLOAD_EXTENSIONS:
        return True

    if "/rec/dologin_pub.asp" in lower_url:
        return True

    if "download" in lower_text:
        return True

    if "pdf" in lower_text:
        return True

    if "source code" in lower_text:
        return True

    if "test vector" in lower_text:
        return True

    if "electronic attachment" in lower_text:
        return True

    return False


def is_page_candidate(url: str) -> bool:
    """Identify ITU pages that may contain more downloadable artifacts."""

    lower_url = url.lower()

    return (
        "/rec/t-rec-" in lower_url
        or "/itu-t/recommendations/rec.aspx" in lower_url
        or "/epublications/publication/itu-t-" in lower_url
        or "/myworkspace/t-signals/vectors" in lower_url
    )


def infer_artifact_type(url: str, link_text: str, content_type: str = "") -> str:
    """Sort artifacts by technical role rather than only by file extension."""

    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    text = f"{url} {link_text} {content_type}".lower()

    if "!!pdf" in text:
        return "recommendation"

    if "!!msw" in text or "!!zwd" in text:
        return "documents"

    if "!!soft" in text:
        return "reference-software"

    if "source" in text or suffix in SOURCE_EXTENSIONS:
        return "source-code"

    if "test vector" in text or "test-vector" in text or "conformance" in text:
        return "test-vectors"

    if "reference software" in text:
        return "reference-software"

    if "annex" in text:
        return "annex"

    if "amendment" in text or "amd" in text:
        return "amendment"

    if "corrigendum" in text or "cor" in text:
        return "corrigendum"

    if suffix in ARCHIVE_EXTENSIONS:
        return "archives"

    if suffix in AUDIO_EXTENSIONS:
        return "audio"

    if suffix in DATA_EXTENSIONS:
        return "data"

    if suffix == ".pdf":
        return "recommendation"

    if suffix in PUBLICATION_EXTENSIONS:
        return "documents"

    return "other"


def api_json(client: Any, url: str, params: dict[str, object] | None = None) -> Any:
    """Fetch JSON from the ITU MyWorkspace API."""

    response = client.get(url, params=params)
    response.raise_for_status()
    return response.json()


def api_text(client: Any, url: str) -> str:
    """Fetch a static ITU HTML page with the shared HTTP client."""

    response = client.get(url)
    response.raise_for_status()
    return response.text


def record_text(record: dict[str, object], *keys: str) -> str:
    """Return the first non-empty string value from an API record."""

    for key in keys:
        value = record.get(key)

        if value is None:
            continue

        text = normalize_text(str(value))

        if text and text != "-":
            return text

    return ""


def record_status(record: dict[str, object]) -> str:
    """Return a normalized ITU Recommendation status field when present."""

    return record_text(
        record,
        "status",
        "Status",
        "rec_status",
        "recommendation_status",
        "state",
        "publication_status",
    )


def is_in_force_record(record: dict[str, object]) -> bool:
    """Treat missing status as active and reject explicit inactive statuses."""

    status = record_status(record).lower()

    if not status:
        return True

    return not any(term in status for term in INACTIVE_RECOMMENDATION_STATUS_TERMS)


def test_signal_id(page: PageRecord) -> str:
    """Return the MWS test-signal ID encoded in a PageRecord."""

    if page.page_id.startswith("ts-"):
        return page.page_id[3:]

    if page.page_id.startswith("val-"):
        return page.page_id[4:]

    return extract_val(page.url)


def edition_from_values(*values: str) -> str:
    """Extract a YYYY-MM edition from API fields or URLs."""

    for value in values:
        parsed = parse_date_to_year_month(value)

        if parsed:
            return parsed

        inferred = infer_edition(value, value)

        if inferred != "unknown-edition":
            return inferred

    return "unknown-edition"


def edition_sort_key(edition: str) -> tuple[int, int]:
    """Return a sortable key for YYYY-MM editions."""

    match = re.match(r"^(19\d{2}|20\d{2})-(0[1-9]|1[0-2])$", edition)

    if not match:
        return (0, 0)

    return (int(match.group(1)), int(match.group(2)))


def latest_pages_by_recommendation(pages: Iterable[PageRecord]) -> list[PageRecord]:
    """Keep the newest page for each Recommendation identifier."""

    latest: dict[tuple[str, str, str], PageRecord] = {}

    for page in pages:
        key = (page.collection, page.series, page.recommendation)
        current = latest.get(key)

        if current is None:
            latest[key] = page
            continue

        if (edition_sort_key(page.edition), page.url) > (
            edition_sort_key(current.edition),
            current.url,
        ):
            latest[key] = page

    return dedupe_pages(latest.values())


def discover_test_signal_pages(client: object) -> list[PageRecord]:
    """Discover ITU-T test-signal vector pages from the MyWorkspace API."""

    payload = api_json(client, TEST_SIGNALS_API_URL)
    pages: list[PageRecord] = []

    if not isinstance(payload, list):
        return pages

    for item in payload:
        if not isinstance(item, dict):
            continue

        raw_id = record_text(item, "ts_id", "id")

        if not raw_id:
            continue

        title = record_text(item, "Title", "title") or f"ITU-T test signal vector {raw_id}"
        recommendation = find_recommendation(
            record_text(item, "Recommendation", "recommendation"),
            title,
        )
        series = recommendation_series(recommendation)
        url = f"{TEST_SIGNALS_URL}/vectors?val={raw_id}"

        pages.append(
            PageRecord(
                collection="test-signals",
                url=url,
                title=title,
                series=series,
                recommendation=recommendation,
                edition="unknown-edition",
                page_id=f"ts-{slugify(raw_id)}",
            )
        )

    return dedupe_pages(pages)


def discover_publication_pages(
    series_values: Iterable[str],
    client: object,
    rows_per_page: int = 200,
    latest_only: bool = True,
) -> list[PageRecord]:
    """Discover ITU-T Recommendation pages from the MyWorkspace API."""

    pages: list[PageRecord] = []

    for series in series_values:
        clean_series = series.strip().upper()

        if not clean_series:
            continue

        page_number = 1

        while True:
            payload = api_json(
                client,
                RECOMMENDATIONS_SEARCH_API_URL,
                params={
                    "query": "",
                    "series": clean_series,
                    "main_edition_flag": "true",
                    "rows": rows_per_page,
                    "page": page_number,
                },
            )

            if isinstance(payload, dict):
                records = payload.get("Data") or []
                total = int(payload.get("Total") or len(records))
            elif isinstance(payload, list):
                records = payload
                total = len(records)
            else:
                break

            if not isinstance(records, list) or not records:
                break

            for item in records:
                if not isinstance(item, dict):
                    continue

                if latest_only and not is_in_force_record(item):
                    continue

                url = record_text(item, "dms_link")

                if not url or not is_allowed_url(url):
                    continue

                rec_name = record_text(item, "rec_name", "name")
                title = record_text(item, "title", "Title") or rec_name
                recommendation = find_recommendation(rec_name, title, url)

                if recommendation == "UNKNOWN":
                    continue

                approval_date = record_text(item, "approval_date")
                edition = edition_from_values(approval_date, rec_name, url)

                pages.append(
                    PageRecord(
                        collection="publications",
                        url=url,
                        title=normalize_text(f"{rec_name} {title}"),
                        series=recommendation_series(recommendation),
                        recommendation=recommendation,
                        edition=edition,
                        page_id=slugify(f"{recommendation.lower()}-{edition}"),
                        edition_group=edition,
                    )
                )

            if page_number * rows_per_page >= total:
                break

            page_number += 1

    if latest_only:
        return [
            replace(page, edition_group="latest") for page in latest_pages_by_recommendation(pages)
        ]

    return dedupe_pages(pages)


def test_signal_downloads(
    client: object,
    page: PageRecord,
    latest_only: bool = True,
) -> tuple[PageRecord, list[tuple[str, str]]]:
    """Return downloadable files for one test-signal API page."""

    payload = api_json(client, TEST_SIGNAL_FILES_API_URL, params={"ts_id": test_signal_id(page)})
    records: list[tuple[str, str, str]] = []

    if not isinstance(payload, list):
        return page, []

    for item in payload:
        if not isinstance(item, dict):
            continue

        url = record_text(item, "file_full_path", "url")

        if not url or not is_allowed_url(url):
            continue

        link_text = record_text(
            item,
            "File description",
            "file_description",
            "File name",
            "file_name",
            "title",
        )
        edition = edition_from_values(
            record_text(item, "Edition", "edition"),
            record_text(item, "main_edition"),
            url,
        )

        records.append((url, link_text or Path(urlparse(url).path).name, edition))

    if latest_only:
        known_editions = [
            edition_sort_key(edition)
            for _, _, edition in records
            if edition_sort_key(edition) != (0, 0)
        ]

        if known_editions:
            latest_edition = max(known_editions)
            records = [
                record for record in records if edition_sort_key(record[2]) == latest_edition
            ]

    downloads = [(url, link_text) for url, link_text, _ in records]
    editions = {edition for _, _, edition in records if edition != "unknown-edition"}

    if len(editions) == 1:
        edition = next(iter(editions))
    elif len(editions) > 1:
        edition = "multiple-editions"
    else:
        edition = page.edition

    return (
        PageRecord(
            collection=page.collection,
            url=page.url,
            title=page.title,
            series=page.series,
            recommendation=page.recommendation,
            edition=edition,
            page_id=page.page_id,
            edition_group=page.edition_group,
        ),
        dedupe_downloads(downloads),
    )


def enrich_page(
    page: PageRecord,
    timeout_ms: int,
    client: object | None = None,
    latest_only: bool = True,
) -> tuple[PageRecord, list[tuple[str, str]]]:
    """Render a page, improve metadata, and return downloadable links."""

    if page.collection == "test-signals" and client is not None:
        return test_signal_downloads(client, page, latest_only)

    html = api_text(client, page.url) if client is not None else render_html(page.url, timeout_ms)
    title = page_title_from_html(html, page.title)
    text = page_text_from_html(html)

    recommendation = page.recommendation

    if recommendation == "UNKNOWN":
        recommendation = find_recommendation(title, text, page.url)

    series = recommendation_series(recommendation)
    edition = infer_edition(text, page.url)

    if edition == "unknown-edition":
        edition = page.edition

    enriched_page = PageRecord(
        collection=page.collection,
        url=page.url,
        title=title,
        series=series,
        recommendation=recommendation,
        edition=edition,
        page_id=page.page_id,
        edition_group=page.edition_group,
    )

    downloads: list[tuple[str, str]] = []

    for url, link_text in parse_html_links(page.url, html):
        if is_download_candidate(url, link_text):
            downloads.append((url, link_text))

    return enriched_page, dedupe_downloads(downloads)


def dedupe_pages(pages: Iterable[PageRecord]) -> list[PageRecord]:
    """Dedupe pages by URL while preserving sorted determinism."""

    by_url = {page.url: page for page in pages}
    return sorted(by_url.values(), key=lambda item: item.url)


def dedupe_downloads(downloads: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Dedupe downloads by URL."""

    by_url: dict[str, str] = {}

    for url, text in downloads:
        by_url[url] = text

    return sorted(by_url.items(), key=lambda item: item[0])


def dedupe_assets(assets: Iterable[AssetRecord]) -> list[AssetRecord]:
    """Dedupe asset records by source URL before scheduling downloads."""

    by_url: dict[str, AssetRecord] = {}

    for asset in assets:
        by_url.setdefault(asset.source_url, asset)

    return sorted(by_url.values(), key=lambda item: item.source_url)


def asset_manifest_record(asset: AssetRecord) -> dict[str, str]:
    """Return the stable discovery-index record for one asset."""

    return {
        "page_url": asset.page.url,
        "source_url": asset.source_url,
        "link_text": asset.link_text,
        "recommendation": asset.page.recommendation,
        "series": asset.page.series,
        "edition": asset.page.edition,
        "collection": asset.page.collection,
    }


def target_directory(root: Path, page: PageRecord, artifact_type: str) -> Path:
    """Build the final folder for one artifact."""

    recommendation = page.recommendation or "UNKNOWN"

    if page.collection == "test-signals":
        return root / "test-signals" / "by-recommendation" / recommendation / artifact_type

    return (
        root
        / "standards"
        / recommendation_category(page)
        / recommendation
        / (page.edition_group or page.edition or "unknown-edition")
        / artifact_type
    )


def filename_from_itu_item_id(url: str) -> str:
    """Derive stable filenames from ITU dologin item IDs."""

    item_id = parse_qs(urlparse(url).query).get("id", [""])[0]

    if not item_id:
        return ""

    lower_id = item_id.lower()
    suffix = ""

    if "!!pdf" in lower_id:
        suffix = ".pdf"
    elif "!!msw" in lower_id:
        suffix = ".doc"
    elif "!!zwd" in lower_id or "!!soft" in lower_id:
        suffix = ".zip"

    if suffix and not lower_id.endswith(suffix):
        item_id = f"{item_id}{suffix}"

    return slugify(item_id, "download.bin")


def filename_from_response(url: str, response: Any) -> str:
    """Derive a stable filename from Content-Disposition or URL."""

    item_id_name = filename_from_itu_item_id(url)

    if item_id_name:
        return item_id_name

    disposition = response.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.IGNORECASE)

    if match:
        return slugify(unquote(match.group(1)), "download.bin")

    parsed_path = unquote(urlparse(str(response.url)).path)
    name = Path(parsed_path).name

    if name:
        return slugify(name, "download.bin")

    digest = hashlib.sha256(str(response.url).encode("utf-8")).hexdigest()[:16]
    return f"download-{digest}.bin"


def should_accept_response(response: Any) -> bool:
    """Reject HTML pages that are not actual downloadable artifacts."""

    content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
    suffix = Path(unquote(urlparse(str(response.url)).path)).suffix.lower()

    if content_type in BINARY_CONTENT_TYPES:
        return True

    if suffix in DOWNLOAD_EXTENSIONS:
        return True

    return False


def unique_path(path: Path) -> Path:
    """Avoid overwriting files when URLs resolve to the same filename."""

    if not path.exists() and not path.with_suffix(path.suffix + ".part").exists():
        return path

    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")

        if (
            not candidate.exists()
            and not candidate.with_suffix(candidate.suffix + ".part").exists()
        ):
            return candidate

    raise RuntimeError(f"Could not allocate unique file path for {path}")


def resumable_paths(path: Path) -> tuple[Path, Path, bool]:
    """Return final/temp paths and whether the final file already exists."""

    temporary_path = path.with_suffix(path.suffix + ".part")

    if path.exists():
        return path, temporary_path, True

    if temporary_path.exists():
        return path, temporary_path, False

    output_path = unique_path(path)
    return output_path, output_path.with_suffix(output_path.suffix + ".part"), False


def sha256_file(path: Path) -> str:
    """Hash a file using bounded memory."""

    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def completed_download_available(record: dict[str, object]) -> bool:
    """Return whether a downloaded manifest record still points at a real file."""

    if record.get("status") != "downloaded":
        return False

    output_path = record.get("output_path")

    if not output_path:
        return False

    path = Path(str(output_path))

    if not path.is_file():
        return False

    expected_size = record.get("size_bytes")

    if expected_size in (None, ""):
        return True

    try:
        size_bytes = int(str(expected_size))
    except (TypeError, ValueError):
        return True

    return size_bytes <= 0 or path.stat().st_size == size_bytes


def load_completed_downloads(manifest_path: Path) -> dict[str, dict[str, object]]:
    """Load completed source URLs whose recorded output files still exist."""

    if not manifest_path.exists():
        return {}

    completed: dict[str, dict[str, object]] = {}

    with manifest_path.open("r", encoding="utf-8") as file_handle:
        for line in file_handle:
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(record, dict):
                continue

            source_url = record.get("source_url")

            if source_url and completed_download_available(record):
                completed[str(source_url)] = record

    return completed


def load_completed_sources(manifest_path: Path) -> set[str]:
    """Load previously downloaded source URLs for resume support."""

    return set(load_completed_downloads(manifest_path))


def write_json(path: Path, payload: object) -> None:
    """Write deterministic JSON for discovery indexes."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, ensure_ascii=False, indent=2, sort_keys=True)


def append_jsonl(path: Path, payload: object) -> None:
    """Append one JSON line."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        file_handle.write("\n")


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def download_artifact(
    client: Any,
    root: Path,
    page: PageRecord,
    source_url: str,
    link_text: str,
    delay: float,
    dry_run: bool,
    shutdown_event: threading.Event | None = None,
) -> DownloadRecord:
    """Download one artifact and return a manifest record."""

    provisional_type = infer_artifact_type(source_url, link_text)
    provisional_dir = target_directory(root, page, provisional_type)

    if dry_run:
        return DownloadRecord(
            collection=page.collection,
            page_url=page.url,
            page_title=page.title,
            series=page.series,
            recommendation=page.recommendation,
            edition=page.edition,
            artifact_type=provisional_type,
            source_url=source_url,
            final_url="",
            output_path=str(provisional_dir),
            content_type="",
            size_bytes=0,
            sha256="",
            status="dry-run",
            downloaded_at_utc=utc_now(),
        )

    if shutdown_requested(shutdown_event):
        raise DownloadInterrupted("shutdown requested before download started")

    time.sleep(delay)

    if shutdown_requested(shutdown_event):
        raise DownloadInterrupted("shutdown requested before download started")

    stream_headers: dict[str, str] = {}
    output_path: Path | None = None
    temporary_path: Path | None = None
    resume_bytes = 0

    if provisional_dir.exists():
        provisional_name = filename_from_itu_item_id(source_url)

        if not provisional_name:
            provisional_name = slugify(Path(unquote(urlparse(source_url).path)).name, "")

        if provisional_name:
            provisional_output = provisional_dir / provisional_name
            provisional_temporary = provisional_output.with_suffix(
                provisional_output.suffix + ".part"
            )

            if provisional_temporary.exists():
                resume_bytes = provisional_temporary.stat().st_size

    if resume_bytes > 0:
        stream_headers["Range"] = f"bytes={resume_bytes}-"

    with client.stream(
        "GET",
        source_url,
        headers=stream_headers or None,
        follow_redirects=True,
    ) as response:
        if resume_bytes > 0 and response.status_code == 416:
            stream_headers = {}
            resume_bytes = 0

        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        artifact_type = infer_artifact_type(str(response.url), link_text, content_type)

        if not should_accept_response(response):
            return DownloadRecord(
                collection=page.collection,
                page_url=page.url,
                page_title=page.title,
                series=page.series,
                recommendation=page.recommendation,
                edition=page.edition,
                artifact_type=artifact_type,
                source_url=source_url,
                final_url=str(response.url),
                output_path=str(target_directory(root, page, artifact_type)),
                content_type=content_type,
                size_bytes=0,
                sha256="",
                status="skipped-non-download",
                downloaded_at_utc=utc_now(),
            )

        output_dir = target_directory(root, page, artifact_type)
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = filename_from_response(source_url, response)
        output_path, temporary_path, already_exists = resumable_paths(output_dir / filename)

        if already_exists:
            return DownloadRecord(
                collection=page.collection,
                page_url=page.url,
                page_title=page.title,
                series=page.series,
                recommendation=page.recommendation,
                edition=page.edition,
                artifact_type=artifact_type,
                source_url=source_url,
                final_url=str(response.url),
                output_path=str(output_path),
                content_type=content_type,
                size_bytes=output_path.stat().st_size,
                sha256=sha256_file(output_path),
                status="skipped-existing-file",
                downloaded_at_utc=utc_now(),
            )

        resumed = temporary_path.exists() and temporary_path.stat().st_size > 0
        append_resume = resumed and response.status_code == 206
        size_bytes = temporary_path.stat().st_size if append_resume else 0

        if resumed and not append_resume:
            temporary_path.unlink(missing_ok=True)

        with temporary_path.open("ab" if append_resume else "wb") as file_handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                if shutdown_requested(shutdown_event):
                    raise DownloadInterrupted("shutdown requested during download")

                if not chunk:
                    continue

                file_handle.write(chunk)
                size_bytes += len(chunk)

        temporary_path.replace(output_path)

    return DownloadRecord(
        collection=page.collection,
        page_url=page.url,
        page_title=page.title,
        series=page.series,
        recommendation=page.recommendation,
        edition=page.edition,
        artifact_type=artifact_type,
        source_url=source_url,
        final_url=str(response.url),
        output_path=str(output_path),
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256_file(output_path),
        status="downloaded",
        downloaded_at_utc=utc_now(),
    )


def crawl(args: argparse.Namespace) -> None:
    """Main crawler and downloader orchestration."""

    output_root = Path(args.out).resolve()
    manifest_path = output_root / "manifest.jsonl"
    errors_path = output_root / "errors.jsonl"
    reporter = ProgressReporter(args.verbose)
    stats = {
        "discovered_pages": 0,
        "pages": 0,
        "rejected_pages": 0,
        "assets": 0,
        "duplicates": 0,
        "downloaded": 0,
        "dry_run": 0,
        "skipped_existing": 0,
        "skipped_existing_file": 0,
        "skipped_non_download": 0,
        "interrupted": 0,
        "errors": 0,
    }
    shutdown_event = threading.Event()
    previous_handlers = install_shutdown_handlers(shutdown_event)

    try:
        output_root.mkdir(parents=True, exist_ok=True)

        headers = {
            "User-Agent": args.user_agent,
            "Accept": "*/*",
        }

        httpx = require_httpx()
        jobs = max(1, args.jobs)
        recommendation_allow_list = selected_recommendations(args)

        with httpx.Client(headers=headers, timeout=args.http_timeout) as client:
            discovered_pages: list[PageRecord] = []
            latest_only = not args.all_editions

            with reporter.progress() as progress:
                discovery_task = progress.add_task("Discovering ITU indexes", total=None)

                if args.include_test_signals:
                    progress.update(discovery_task, description="Discovering test-signal catalog")
                    discovered_pages.extend(discover_test_signal_pages(client))

                    if args.test_signal_val_range:
                        discovered_pages.extend(
                            build_test_signal_range_pages(args.test_signal_val_range)
                        )

                if args.include_publications:
                    series_values = series_values_for_publications(args, recommendation_allow_list)
                    progress.update(
                        discovery_task,
                        description=f"Discovering publication catalog ({','.join(series_values)})",
                    )
                    discovered_pages.extend(
                        discover_publication_pages(series_values, client, latest_only=latest_only)
                    )

                discovered_pages = dedupe_pages(discovered_pages)
                accepted_pages, rejected_pages = filter_pages_by_recommendations(
                    discovered_pages,
                    recommendation_allow_list,
                )
                stats["discovered_pages"] = len(discovered_pages)
                stats["pages"] = len(accepted_pages)
                stats["rejected_pages"] = len(rejected_pages)
                progress.update(
                    discovery_task,
                    description=f"Accepted {len(accepted_pages)} of {len(discovered_pages)} pages",
                    total=1,
                    completed=1,
                )

                write_json(
                    output_root / "index" / "discovered-pages.json",
                    [asdict(page) for page in discovered_pages],
                )
                write_json(
                    output_root / "index" / "accepted-pages.json",
                    [asdict(page) for page in accepted_pages],
                )
                write_json(output_root / "index" / "rejected-pages.json", rejected_pages)

                completed_downloads = load_completed_downloads(manifest_path)
                discovered_assets: list[AssetRecord] = []
                page_task = progress.add_task(
                    "Scanning pages for assets", total=len(accepted_pages)
                )

                for raw_page in accepted_pages:
                    if shutdown_event.is_set():
                        break

                    progress.update(
                        page_task,
                        description=f"Scanning {raw_page.collection} {raw_page.recommendation}",
                    )
                    try:
                        page, downloads = enrich_page(
                            raw_page, args.timeout_ms, client, latest_only
                        )
                    except Exception as error:
                        stats["errors"] += 1
                        append_jsonl(
                            errors_path,
                            {
                                "page_url": raw_page.url,
                                "status": f"page-error: {type(error).__name__}: {error}",
                                "at_utc": utc_now(),
                            },
                        )
                        progress.advance(page_task)
                        continue

                    for source_url, link_text in downloads:
                        discovered_assets.append(AssetRecord(page, source_url, link_text))

                    if args.verbose:
                        reporter.log(
                            f"[page] {page.collection} {page.recommendation} {page.edition} "
                            f"downloads={len(downloads)}"
                        )

                    progress.advance(page_task)

                unique_assets = dedupe_assets(discovered_assets)
                stats["assets"] = len(unique_assets)
                stats["duplicates"] = len(discovered_assets) - len(unique_assets)
                discovered_downloads = [asset_manifest_record(asset) for asset in unique_assets]
                write_json(
                    output_root / "index" / "discovered-downloads.json", discovered_downloads
                )

                download_task = progress.add_task(
                    f"Downloading assets ({jobs} workers)",
                    total=len(unique_assets),
                )
                pending_assets: list[AssetRecord] = []

                for asset in unique_assets:
                    if asset.source_url in completed_downloads and not args.force:
                        stats["skipped_existing"] += 1

                        if args.verbose:
                            reporter.log(
                                f"[skip] already downloaded {asset.source_url}", style="yellow"
                            )

                        progress.advance(download_task)
                    else:
                        pending_assets.append(asset)

                asset_iterator = iter(pending_assets)
                active: dict[concurrent.futures.Future, AssetRecord] = {}
                exhausted = False

                def submit_next_download(executor: concurrent.futures.ThreadPoolExecutor) -> None:
                    nonlocal exhausted

                    try:
                        asset = next(asset_iterator)
                    except StopIteration:
                        exhausted = True
                        return

                    future = executor.submit(
                        download_artifact,
                        client,
                        output_root,
                        asset.page,
                        asset.source_url,
                        asset.link_text,
                        args.delay,
                        args.dry_run,
                        shutdown_event,
                    )
                    active[future] = asset

                with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
                    while active or not exhausted:
                        while not shutdown_event.is_set() and not exhausted and len(active) < jobs:
                            submit_next_download(executor)

                        if not active:
                            break

                        done, _ = concurrent.futures.wait(
                            active,
                            timeout=0.25,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )

                        if not done:
                            if shutdown_event.is_set():
                                exhausted = True
                                reporter.log(
                                    "Shutdown requested; waiting for active downloads to stop...",
                                    style="yellow",
                                )

                            continue

                        for future in done:
                            asset = active.pop(future)
                            progress.update(
                                download_task, description=f"Handled {asset.page.recommendation}"
                            )

                            try:
                                record = future.result()
                            except DownloadInterrupted:
                                stats["interrupted"] += 1
                            except Exception as error:
                                stats["errors"] += 1
                                append_jsonl(
                                    errors_path,
                                    {
                                        "collection": asset.page.collection,
                                        "page_url": asset.page.url,
                                        "source_url": asset.source_url,
                                        "recommendation": asset.page.recommendation,
                                        "series": asset.page.series,
                                        "edition": asset.page.edition,
                                        "status": f"download-error: {type(error).__name__}: {error}",
                                        "at_utc": utc_now(),
                                    },
                                )
                            else:
                                append_jsonl(manifest_path, asdict(record))

                                if record.status == "downloaded":
                                    stats["downloaded"] += 1
                                elif record.status == "dry-run":
                                    stats["dry_run"] += 1
                                elif record.status == "skipped-existing-file":
                                    stats["skipped_existing_file"] += 1
                                elif record.status == "skipped-non-download":
                                    stats["skipped_non_download"] += 1

                                if args.verbose:
                                    reporter.log(
                                        f"[{record.status}] {record.output_path}", style="green"
                                    )

                            progress.advance(download_task)

                        if shutdown_event.is_set():
                            exhausted = True

                if shutdown_event.is_set():
                    reporter.log(
                        "Graceful shutdown complete. Partial .part files are resumable.",
                        style="yellow",
                    )

    finally:
        restore_shutdown_handlers(previous_handlers)

    reporter.summary(stats, output_root)


def build_test_signal_range_pages(value: str) -> list[PageRecord]:
    """Create test-signal vector pages from an explicit numeric range."""

    start_text, end_text = value.split(":", maxsplit=1)
    start = int(start_text)
    end = int(end_text)

    if start > end:
        raise ValueError("--test-signal-val-range must use start:end with start <= end")

    pages: list[PageRecord] = []

    for vector_id in range(start, end + 1):
        pages.append(
            PageRecord(
                collection="test-signals",
                url=f"{TEST_SIGNALS_URL}/vectors?val={vector_id}",
                title=f"ITU-T test signal vector {vector_id}",
                series="UNKNOWN",
                recommendation="UNKNOWN",
                edition="unknown-edition",
                page_id=f"val-{vector_id}",
            )
        )

    return pages


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(
        description="Download ITU-T test signals and publications into a structured archive."
    )

    parser.add_argument("--out", default="research/itu-archive", help="Output directory.")

    parser.add_argument(
        "--series",
        default="",
        help=(
            "Optional comma-separated ITU-T series limit. Defaults to the active "
            "profile's series, or G,H,J,P,T,V with --download-all."
        ),
    )

    parser.add_argument(
        "--profile",
        choices=("codecs", "all"),
        default="codecs",
        help="Recommendation profile to download. Default: codecs.",
    )

    parser.add_argument(
        "--allow-list",
        default="",
        help="Path to a custom Recommendation allow-list, one identifier per line.",
    )

    parser.add_argument(
        "--download-all",
        action="store_true",
        help="Use the old broad series crawl instead of the codec Recommendation profile.",
    )

    parser.add_argument(
        "--include-test-signals",
        action="store_true",
        help="Discover and download ITU-T test-signal vectors.",
    )

    parser.add_argument(
        "--include-publications",
        action="store_true",
        help="Discover and download ITU-T Recommendation publications.",
    )

    parser.add_argument(
        "--test-signal-val-range",
        default="",
        help="Optional explicit test-signal vector range, for example 1:500.",
    )

    parser.add_argument(
        "--delay", type=float, default=1.5, help="Delay between downloads in seconds."
    )

    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=60_000,
        help="Playwright page-render timeout in milliseconds.",
    )

    parser.add_argument(
        "--http-timeout", type=float, default=180.0, help="HTTP timeout in seconds."
    )

    parser.add_argument(
        "--user-agent", default="itu-t-archive-downloader/1.0", help="HTTP User-Agent header."
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover pages and links without downloading payloads.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Download URLs even when manifest says they were already downloaded.",
    )

    parser.add_argument(
        "--jobs", type=int, default=4, help="Maximum number of concurrent downloads."
    )

    parser.add_argument(
        "--all-editions",
        action="store_true",
        help="Keep all discovered publication and test-signal editions instead of only the latest.",
    )

    parser.add_argument("--verbose", action="store_true", help="Print progress.")

    return parser.parse_args()


def main() -> None:
    """CLI entry point."""

    args = parse_args()

    if args.download_all and args.allow_list:
        raise SystemExit("--download-all cannot be combined with --allow-list")

    if not args.include_test_signals and not args.include_publications:
        raise SystemExit("Select at least one of --include-test-signals or --include-publications")

    try:
        crawl(args)
    except MissingDependencyError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
