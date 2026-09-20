#!/usr/bin/env python3
"""Scheduled deep-crawl collector for the Daily Brief source catalog.

Reads ``source.json`` (next to this script), crawls every source whose last
*successful* run is older than its declared ``frequency``, records each attempt
in a SQLite log, and keeps only the latest good result for each source on disk.

Collection philosophy: prefer too much over too little, cheapest source first.
For each source the crawler fetches its feeds (RSS/Atom/ICS/JSON) over plain
HTTP, then deep-crawls its pages with crawl4ai's *HTTP* strategy, and only falls
back to the headless-browser strategy when the HTTP result looks blocked, empty,
JS-shell-like, or undated. Every input is rendered through the same crawl4ai
HTML-to-markdown pipeline, so one common ``content.md`` / ``meta.json`` output
is produced regardless of where the bytes came from. Relevance filtering is
expected to happen later, downstream.

Layout
------
    data-collect/
        source.json                 source catalog (input)
        crawl_log.db                SQLite run log (all attempts)
        crawl/<slug>/content.md     latest good combined markdown
        crawl/<slug>/meta.json      latest good result metadata (per page/asset)
        crawl/<slug>/html/<n>.html  optional raw HTML (--save-html)

Usage
-----
    python3 crawl_sources.py                  # crawl sources that are due
    python3 crawl_sources.py --force          # crawl every source now
    python3 crawl_sources.py --dry-run        # show what would be crawled
    python3 crawl_sources.py --status         # last run per source
    python3 crawl_sources.py --errors         # sources whose latest run failed
    python3 crawl_sources.py --prune 50       # keep only last 50 rows/source
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html
import hashlib
import io
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

UTC = timezone.utc
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_FILE = SCRIPT_DIR / "source.json"
DEFAULT_DB = SCRIPT_DIR / "crawl_log.db"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "crawl"

FREQUENCY_SECONDS: dict[str, int] = {
    "hourly": 60 * 60,
    "4hours": 4 * 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
    "monthly": 30 * 24 * 60 * 60,
}
DEFAULT_FREQUENCY = "daily"
DEFAULT_ENGINE = "auto"
ENGINES = ("auto", "browser", "http", "api")


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from ``.env`` without overriding real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(SCRIPT_DIR / ".env")

# URL keywords that steer the best-first deep crawl toward dated content.
# These only affect crawl *order* (what gets crawled first); nothing is
# excluded, so deep enough crawls still collect everything reachable.
SEED_KEYWORDS = [
    "event", "events", "calendar", "schedule", "shows", "show",
    "upcoming", "happening", "concert", "performance", "exhibit",
    "exhibition", "program", "programs", "festival", "meeting",
    "agenda", "notice", "news", "article", "story", "stories",
    "press", "announcement", "community", "today", "week", "month",
    # Ticketing/detail pages carry the richest event data (and often the
    # JSON-LD Event markup the funnel parses structurally).
    "ticket", "tickets", "eventbrite", "etix", "showclix",
]

# Non-HTML resources worth harvesting: they frequently carry dated events.
ASSET_EXTENSIONS = {".pdf", ".ics", ".xml", ".rss", ".atom", ".json"}
# Feed tuning (see CRAWLER-RECOMMENDATIONS.md).
FEED_LINK_LIMIT = 20       # max feed item links fetched per source (R2)
FEED_LINKS_IN_META = 30    # max links/item URLs recorded per feed in meta.json (R5)
FEED_SUMMARY_MAX = 20000   # safety cap on a single item's summary text
FEED_FRESH_DAYS = 7        # feed freshness window for suppressing the browser (R4)
# Date-like tokens used to judge whether collected content is actually dated.
DATE_TOKEN_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}:\d{2}\s*(?:am|pm)\b",
    re.IGNORECASE,
)
ASSET_URL_RE = re.compile(r"(?:https?|webcal)://[^\s)\]\"'<>]+", re.IGNORECASE)
_ICAL_QUERY_RE = re.compile(
    r"(?:^|[&;])(?:format=ical|feed=ical|ical(?:=1|=true)?)(?=$|[&;])",
    re.IGNORECASE,
)
MAX_ASSET_BYTES = 30 * 1024 * 1024
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# pypdf is noisy about malformed/stale PDF links; those are handled per-URL.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# curl_cffi impersonates Chrome's TLS/JA3 fingerprint and clears WAFs that block
# plain HTTP clients (Cloudflare bot management, DataDome, etc.). Optional.
try:
    from curl_cffi import requests as _curl_requests
except Exception:  # noqa: BLE001
    _curl_requests = None

# Optional commercial scraping-API fallback (last resort only). Providers are
# configured by name + key; `generic` uses SCRAPER_API_TEMPLATE with {url}/{key}.
API_PROVIDERS = ("none", "scraperapi", "scrapingbee", "scrapingant", "generic")
API_RENDER_FLAG = {
    "scraperapi": "render",
    "scrapingbee": "render_js",
    "scrapingant": "browser",
}
HTTP_HEADERS = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name    TEXT    NOT NULL,
    source_url     TEXT    NOT NULL,
    trigger        TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT    NOT NULL,
    duration_ms    INTEGER NOT NULL,
    success        INTEGER NOT NULL,
    status_code    INTEGER,
    final_url      TEXT,
    title          TEXT,
    content_bytes  INTEGER,
    pages_crawled  INTEGER,
    pages_failed   INTEGER,
    assets_fetched INTEGER,
    word_count     INTEGER,
    output_dir     TEXT,
    backend        TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source_started
    ON runs (source_name, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_success
    ON runs (success, started_at DESC);
"""


# --------------------------------------------------------------------------- #
# time / formatting helpers
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def human_delta(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return "never"
    seconds = (now - dt).total_seconds()
    if seconds < 0:
        return "now"
    for label, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            return f"{int(seconds // span)}{label} ago"
    return f"{int(seconds)}s ago"


def human_until(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return "due now"
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return "overdue"
    for label, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            return f"in {int(seconds // span)}{label}"
    return f"in {int(seconds)}s"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# source catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Source:
    name: str
    url: str
    frequency: str
    locality_index: Optional[int] = None
    location: Optional[str] = None
    category: Optional[str] = None
    type: Optional[str] = None
    events_url: Optional[str] = None
    description: Optional[str] = None
    engine: str = DEFAULT_ENGINE  # "auto", "browser", or "http"
    crawl_urls: tuple[str, ...] = ()  # extra seeds for the browser/http engine
    feed_urls: tuple[str, ...] = ()  # rss/atom/ics/json fetched over plain HTTP
    max_pages: Optional[int] = None  # per-source override of --max-pages

    @property
    def period_seconds(self) -> int:
        return FREQUENCY_SECONDS.get(self.frequency, FREQUENCY_SECONDS[DEFAULT_FREQUENCY])

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "locality_index": self.locality_index,
            "location": self.location,
            "category": self.category,
            "type": self.type,
            "frequency": self.frequency,
            "engine": self.engine,
            "url": self.url,
            "events_url": self.events_url,
            "crawl_urls": list(self.crawl_urls),
            "feed_urls": list(self.feed_urls),
            "max_pages": self.max_pages,
        }


def load_sources(path: Path) -> list[Source]:
    if not path.exists():
        raise SystemExit(f"error: source catalog not found: {path}")
    document = json.loads(path.read_text())
    raw = document.get("sources") if isinstance(document, dict) else document
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"error: no 'sources' array in {path}")

    sources: list[Source] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        name = (item.get("name") or "").strip()
        url = (item.get("url") or "").strip()
        if not name or not url:
            raise SystemExit(f"error: source #{index} in {path} is missing name/url")
        if name in seen:
            raise SystemExit(f"error: duplicate source name in {path}: {name!r}")
        seen.add(name)
        frequency = (item.get("frequency") or DEFAULT_FREQUENCY).strip().lower()
        if frequency not in FREQUENCY_SECONDS:
            print(
                f"warning: {name!r} has unknown frequency {frequency!r}; "
                f"treating as {DEFAULT_FREQUENCY}",
                file=sys.stderr,
            )
            frequency = DEFAULT_FREQUENCY
        engine = (item.get("engine") or DEFAULT_ENGINE).strip().lower()
        if engine not in ENGINES:
            print(
                f"warning: {name!r} has unknown engine {engine!r}; "
                f"treating as {DEFAULT_ENGINE}",
                file=sys.stderr,
            )
            engine = DEFAULT_ENGINE
        sources.append(
            Source(
                name=name,
                url=url,
                frequency=frequency,
                locality_index=item.get("locality_index"),
                location=item.get("location") or item.get("locality"),
                category=item.get("category"),
                type=item.get("type"),
                events_url=item.get("events_url"),
                description=item.get("description"),
                engine=engine,
                crawl_urls=_as_url_list(item.get("crawl_urls")),
                feed_urls=_as_url_list(item.get("feed_urls")),
                max_pages=item.get("max_pages"),
            )
        )
    return sources


def _as_url_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in value if str(v).strip())


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:80].strip("-")
    return slug or hashlib.sha1(name.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# SQLite log
# --------------------------------------------------------------------------- #
def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "assets_fetched" not in columns:  # migrate older logs
        conn.execute("ALTER TABLE runs ADD COLUMN assets_fetched INTEGER")
    conn.commit()
    return conn


@dataclass
class RunRecord:
    source_name: str
    source_url: str
    trigger: str
    started_at: datetime
    duration_ms: int
    success: bool
    status_code: Optional[int] = None
    final_url: Optional[str] = None
    title: Optional[str] = None
    content_bytes: Optional[int] = None
    pages_crawled: Optional[int] = None
    pages_failed: Optional[int] = None
    assets_fetched: Optional[int] = None
    word_count: Optional[int] = None
    output_dir: Optional[str] = None
    backend: Optional[str] = None
    error: Optional[str] = None
    finished_at: datetime = field(default_factory=utc_now)


def insert_run(conn: sqlite3.Connection, record: RunRecord) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            source_name, source_url, trigger, started_at, finished_at,
            duration_ms, success, status_code, final_url, title,
            content_bytes, pages_crawled, pages_failed, assets_fetched,
            word_count, output_dir, backend, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.source_name,
            record.source_url,
            record.trigger,
            to_iso(record.started_at),
            to_iso(record.finished_at),
            record.duration_ms,
            int(record.success),
            record.status_code,
            record.final_url,
            record.title,
            record.content_bytes,
            record.pages_crawled,
            record.pages_failed,
            record.assets_fetched,
            record.word_count,
            record.output_dir,
            record.backend,
            record.error,
        ),
    )
    conn.commit()


def latest_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT r.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY source_name ORDER BY started_at DESC, id DESC
                   ) AS rn
            FROM runs r
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["source_name"]: row for row in rows}


def last_successes(conn: sqlite3.Connection) -> dict[str, datetime]:
    rows = conn.execute(
        """
        SELECT source_name, MAX(finished_at) AS last_ok
        FROM runs
        WHERE success = 1
        GROUP BY source_name
        """
    ).fetchall()
    result: dict[str, datetime] = {}
    for row in rows:
        parsed = parse_iso(row["last_ok"])
        if parsed:
            result[row["source_name"]] = parsed
    return result


def prune_runs(conn: sqlite3.Connection, keep: int) -> int:
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.execute(
        """
        DELETE FROM runs
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_name ORDER BY started_at DESC, id DESC
                       ) AS rn
                FROM runs
            )
            WHERE rn <= ?
        )
        """,
        (keep,),
    )
    conn.commit()
    return before - conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


# --------------------------------------------------------------------------- #
# crawl4ai backend
# --------------------------------------------------------------------------- #
@dataclass
class PageResult:
    url: str
    final_url: Optional[str]
    status_code: Optional[int]
    title: Optional[str]
    markdown: str
    html: Optional[str]
    depth: Optional[int]
    success: bool
    error: Optional[str]
    kind: str = "page"  # "page" (HTML) or "asset" (pdf/ics/feed/json)
    feed: Optional[dict] = None  # parsed feed summary when this is a feed
    images: list[str] = field(default_factory=list)  # og:image/twitter:image etc.


@dataclass
class CrawlOutcome:
    success: bool
    pages: list[PageResult] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    auto_feeds: list[str] = field(default_factory=list)
    feeds: list[dict] = field(default_factory=list)
    browser_used: bool = False
    api_used: bool = False
    seed_status_code: Optional[int] = None
    seed_final_url: Optional[str] = None
    seed_title: Optional[str] = None
    error: Optional[str] = None

    @property
    def pages_failed(self) -> int:
        return sum(1 for page in self.pages if page.kind == "page" and not page.success)

    @property
    def assets_fetched(self) -> int:
        return sum(
            1 for page in self.pages if page.kind == "asset" and page.success
        )


class Crawl4AIBackend:
    """Quality-focused wrapper around one shared AsyncWebCrawler.

    Extremely permissive: relevance-scored deep crawl of same-domain links,
    raw (unpruned) markdown, iframes and lazy content included, plus harvesting
    of linked PDF/ICS/RSS/JSON resources.
    """

    def __init__(
        self,
        *,
        timeout_s: int,
        max_depth: int,
        max_pages: int,
        max_assets: int,
        fetch_assets: bool,
        magic: bool,
        stealth: bool,
        retries: int,
        api_provider: str = "none",
        api_key: str = "",
        api_template: str = "",
        api_render: bool = True,
        api_concurrency: int = 1,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.max_assets = max_assets
        self.fetch_assets = fetch_assets
        self.magic = magic
        self.stealth = stealth
        self.retries = retries
        self.api_provider = api_provider if api_provider in API_PROVIDERS else "none"
        self.api_key = api_key
        self.api_template = api_template
        self.api_render = api_render
        self.api_concurrency = max(1, api_concurrency)
        if self.api_provider != "none" and not (self.api_key or self.api_template):
            self.api_provider = "none"  # no credentials -> disabled
        self.name = crawl4ai_version()
        self._browser: Any = None
        self._http: Any = None
        self._api_semaphore: Any = None
        self._http_lock: Any = None
        self._browser_lock: Any = None

    async def __aenter__(self) -> "Crawl4AIBackend":
        # One shared API limiter for the whole run, so the plan's concurrency
        # cap (e.g. ScrapingAnt free = 1) is never exceeded across sources.
        self._api_semaphore = asyncio.Semaphore(self.api_concurrency)
        # The backend is shared across concurrently-crawled sources, so crawler
        # startup must be serialized (check-then-await is otherwise a race).
        self._http_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for crawler in (self._browser, self._http):
            if crawler is not None:
                try:
                    await crawler.close()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = None
        self._http = None

    async def _ensure_browser(self) -> Any:
        async with self._browser_lock:
            if self._browser is None:
                from crawl4ai import AsyncWebCrawler, BrowserConfig

                browser = BrowserConfig(
                    headless=True,
                    verbose=False,
                    enable_stealth=self.stealth,
                )
                self._browser = AsyncWebCrawler(config=browser)
                await self._browser.start()
        return self._browser

    async def _ensure_http(self) -> Any:
        async with self._http_lock:
            if self._http is None:
                from crawl4ai import AsyncWebCrawler, HTTPCrawlerConfig
                from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

                config = HTTPCrawlerConfig(headers=HTTP_HEADERS, follow_redirects=True)
                self._http = AsyncWebCrawler(
                    crawler_strategy=AsyncHTTPCrawlerStrategy(browser_config=config)
                )
                await self._http.start()
        return self._http

    def _run_config(self, max_pages: int, *, browser: bool) -> Any:
        from crawl4ai import (
            BestFirstCrawlingStrategy,
            CacheMode,
            CompositeScorer,
            ContentTypeFilter,
            CrawlerRunConfig,
            DefaultMarkdownGenerator,
            FilterChain,
            FreshnessScorer,
            KeywordRelevanceScorer,
        )

        scorer = CompositeScorer(
            [
                KeywordRelevanceScorer(keywords=SEED_KEYWORDS, weight=1.0),
                FreshnessScorer(weight=0.4, current_year=utc_now().year),
            ]
        )
        # Keep the HTML page budget for HTML: non-HTML (pdf/ics/xml/json/images)
        # is rejected from traversal and harvested separately via HTTP instead.
        filter_chain = FilterChain(
            [ContentTypeFilter(allowed_types=["text/html", "application/xhtml+xml"])]
        )
        strategy = BestFirstCrawlingStrategy(
            max_depth=self.max_depth,
            max_pages=max_pages,
            include_external=False,
            filter_chain=filter_chain,
            url_scorer=scorer,
        )
        options: dict[str, Any] = dict(
            cache_mode=CacheMode.BYPASS,
            # No content filter: keep the full, unfiltered markdown.
            markdown_generator=DefaultMarkdownGenerator(),
            deep_crawl_strategy=strategy,
            page_timeout=self.timeout_s * 1000,
            max_retries=self.retries,
            verbose=False,
        )
        if browser:
            # Browser-only options; these are what force crawl4ai to route
            # through Chromium, so they are set only for the browser strategy.
            options.update(
                magic=self.magic,
                remove_overlay_elements=True,
                remove_consent_popups=True,
                scan_full_page=True,
                process_iframes=True,
            )
        return CrawlerRunConfig(**options)

    async def _crawl_http(
        self, seeds: list[str], max_pages: int
    ) -> tuple[list[PageResult], list[str]]:
        hard_timeout = self.timeout_s + max(1, len(seeds)) * max_pages * self.timeout_s
        crawler = await self._ensure_http()
        return await self._crawl(
            crawler, seeds, self._run_config(max_pages, browser=False), hard_timeout
        )

    async def _crawl_browser(
        self, seeds: list[str], max_pages: int
    ) -> tuple[list[PageResult], list[str]]:
        hard_timeout = self.timeout_s + max(1, len(seeds)) * max_pages * self.timeout_s
        crawler = await self._ensure_browser()
        return await self._crawl(
            crawler, seeds, self._run_config(max_pages, browser=True), hard_timeout
        )

    async def _crawl(
        self, crawler: Any, seeds: list[str], config: Any, hard_timeout: int
    ) -> tuple[list[PageResult], list[str]]:
        pages: list[PageResult] = []
        errors: list[str] = []
        for seed in seeds:
            try:
                raw = await asyncio.wait_for(
                    crawler.arun(url=seed, config=config), timeout=hard_timeout
                )
            except asyncio.TimeoutError:
                errors.append(f"{seed}: hard timeout after {hard_timeout}s")
                continue
            except Exception as exc:  # noqa: BLE001 - surfaced to the log
                errors.append(f"{seed}: {type(exc).__name__}: {exc}")
                continue
            pages.extend(_to_page(result, seed) for result in _as_result_list(raw))
        return pages, errors

    async def _api_many(
        self, urls: list[str], limit: Optional[int] = None
    ) -> list[PageResult]:
        urls = _dedupe_strings(urls)
        if limit is not None:
            urls = urls[:limit]
        if not urls or self.api_provider == "none":
            return []
        # Many plans cap concurrency (e.g. ScrapingAnt free = 1); the limiter is
        # shared across all sources in the run.
        semaphore = self._api_semaphore or asyncio.Semaphore(self.api_concurrency)

        async def one(url: str) -> PageResult:
            async with semaphore:
                try:
                    return await asyncio.to_thread(
                        _fetch_via_api,
                        url,
                        self.timeout_s,
                        self.api_provider,
                        self.api_key,
                        self.api_template,
                        self.api_render,
                    )
                except Exception as exc:  # noqa: BLE001
                    return _asset_failure(
                        url,
                        url,
                        None,
                        _mask_secret(f"api: {type(exc).__name__}: {exc}", self.api_key),
                        "page",
                    )

        return list(await asyncio.gather(*(one(u) for u in urls)))

    async def _retry_blocked(
        self, fetch: Any, urls: list[str], attempts: int = 3, base_delay: float = 1.5
    ) -> list[PageResult]:
        """Retry URLs whose fetch came back empty/blocked, with backoff.

        Anti-bot blocks are often transient; a couple of spaced retries recover
        them without spending API credits or a browser.
        """
        results: list[PageResult] = []
        delay = base_delay
        for attempt in range(1, attempts + 1):
            results = await fetch(urls)
            if any(p.success and p.markdown.strip() for p in results):
                return results
            if attempt < attempts:
                await asyncio.sleep(delay + random.uniform(0, 0.75))
                delay *= 2
        return results

    async def _http_many(
        self, urls: list[str], limit: Optional[int] = None
    ) -> list[PageResult]:
        urls = _dedupe_strings(urls)
        if limit is not None:
            urls = urls[:limit]
        if not urls:
            return []
        semaphore = asyncio.Semaphore(6)

        async def one(url: str) -> PageResult:
            async with semaphore:
                try:
                    return await asyncio.to_thread(_fetch_resource, url, self.timeout_s)
                except Exception as exc:  # noqa: BLE001 - one bad URL never stops the run
                    return _asset_failure(
                        url, url, None, f"{type(exc).__name__}: {exc}", "page"
                    )

        return list(await asyncio.gather(*(one(u) for u in urls)))

    async def fetch(self, source: "Source") -> CrawlOutcome:
        seeds = _browser_seeds_for(source)
        html_seeds = [s for s in seeds if _extension(s) not in ASSET_EXTENSIONS]
        asset_seeds = [s for s in seeds if _extension(s) in ASSET_EXTENSIONS]
        max_pages = source.max_pages or self.max_pages

        pages: list[PageResult] = []
        errors: list[str] = []
        browser_used = False
        api_used = False

        # Feeds first: cheap, structured, dated. A working feed usually means
        # the heavy browser tier is not needed.
        # Feeds are configuration, not optional assets: always fetched, and
        # independent of --no-assets (which only disables discovered/linked
        # resources). This is the default RSS/Atom path.
        feed_pages = await self._http_many(
            _feed_urls_for(source), limit=self.max_assets
        )
        pages.extend(feed_pages)
        feed_summaries = [p.feed for p in feed_pages if p.feed]
        feed_items = sum(int(f.get("items") or 0) for f in feed_summaries)
        newest_feed = max((f.get("newest") or "" for f in feed_summaries), default="")
        feed_covered = feed_items >= 3 and _is_recent(newest_feed, FEED_FRESH_DAYS)

        # R2: pull the feed's article links into the corpus directly, so article
        # pages exist without a second downstream fetch.
        feed_links = _dedupe_strings(
            link
            for summary in feed_summaries
            for link in (summary.get("articles") or [])
        )
        if feed_links:
            pages.extend(
                await self._http_many(
                    feed_links, limit=min(max_pages, FEED_LINK_LIMIT)
                )
            )

        if source.engine == "api":
            api_pages = await self._api_many(html_seeds)
            pages.extend(api_pages)
            api_used = bool(any(p.success for p in api_pages))
        elif source.engine == "browser":
            browser_pages, browser_errors = await self._crawl_browser(
                html_seeds, max_pages
            )
            pages.extend(browser_pages)
            errors.extend(browser_errors)
            browser_used = True
        else:
            # "http" or "auto": start with the lightweight HTTP pipeline.
            http_pages, http_errors = await self._crawl_http(html_seeds, max_pages)
            errors.extend(http_errors)
            # crawl4ai's HTTP client cannot impersonate Chrome's TLS, so retry
            # blocked/empty seeds with curl_cffi before considering the browser.
            curl_fallback = False
            if _needs_curl_fallback(http_pages, http_errors):
                curl_pages = await self._retry_blocked(self._http_many, html_seeds)
                if any(p.success and p.markdown.strip() for p in curl_pages):
                    curl_fallback = True
                    http_pages.extend(curl_pages)
            pages.extend(http_pages)

            should_escalate = _needs_browser(http_pages, http_errors)
            # A curl_cffi retry that returned dated content is a real result.
            if curl_fallback and _has_dated_content(http_pages):
                should_escalate = False
            # R4: fresh feeds suppress browser escalation for non-event sources;
            # event sources still escalate when their calendar looks uncovered.
            if feed_covered and not _is_event_source(source):
                should_escalate = False
            if source.engine == "auto" and should_escalate:
                browser_pages, browser_errors = await self._crawl_browser(
                    html_seeds, max_pages
                )
                pages.extend(browser_pages)
                errors.extend(browser_errors)
                browser_used = True

        # Non-HTML resources: explicit feeds, asset seeds, linked assets, and
        # feeds advertised in <head> (rss/atom). Bounded by --max-assets.
        auto_feeds = _discover_feeds_in_pages(pages)
        if self.fetch_assets:
            already = {_normalize_url(p.url) for p in pages}
            candidates = _dedupe_strings(
                asset_seeds
                + _harvest_asset_urls(pages)
                + auto_feeds
            )
            candidates = [u for u in candidates if _normalize_url(u) not in already]
            pages.extend(await self._http_many(candidates, limit=self.max_assets))

        # Structured data embedded in any HTML we collected (schema.org etc.).
        pages.extend(_jsonld_pages(pages))

        # Commercial scraping API: last resort, only when the mechanical tiers
        # (feeds, HTTP, curl_cffi, browser) still produced nothing usable.
        if (
            self.api_provider != "none"
            and source.engine != "api"
            and _still_failing(pages)
        ):
            api_pages = await self._retry_blocked(self._api_many, html_seeds, attempts=2)
            if any(p.success and p.markdown.strip() for p in api_pages):
                pages.extend(api_pages)
                api_used = True

        pages = _dedupe_pages(pages)
        if not pages:
            return CrawlOutcome(
                success=False, seeds=seeds, error="; ".join(errors) or "no results"
            )

        success = any(page.success and page.markdown.strip() for page in pages)
        error: Optional[str] = None
        if not success:
            error = next((p.error for p in pages if p.error), None)
            if not error:
                error = "; ".join(errors) or "no content extracted"

        main_seed = _normalize_url(source.url)
        seed_page = next(
            (
                page
                for page in pages
                if _normalize_url(page.url) == main_seed
                or _normalize_url(page.final_url or "") == main_seed
            ),
            pages[0],
        )

        return CrawlOutcome(
            success=success,
            pages=pages,
            seeds=seeds,
            auto_feeds=auto_feeds,
            feeds=feed_summaries,
            browser_used=browser_used,
            api_used=api_used,
            seed_status_code=seed_page.status_code,
            seed_final_url=seed_page.final_url,
            seed_title=seed_page.title,
            error=error,
        )


def _needs_browser(pages: list[PageResult], errors: list[str]) -> bool:
    """Decide whether the lightweight HTTP result needs the browser fallback.

    Escalates when the HTTP crawl looks blocked, empty, or JS-shell-like: a
    server-rendered site yields several content pages, while a JS shell yields
    only the seed(s) with navigation text.
    """
    if not pages:
        return True
    if any(page.status_code in (401, 403, 429) for page in pages):
        return True
    content_pages = [
        page
        for page in pages
        if page.kind == "page" and page.success and page.markdown.strip()
    ]
    if not content_pages:
        return True
    total_words = sum(len(page.markdown.split()) for page in content_pages)
    if len(content_pages) < 3 or total_words < 500:
        return True
    # Many crawled pages but little text on each = JS shells, not articles.
    if total_words / len(content_pages) < 200:
        return True
    # Nothing date-like: not useful for an events/news brief, so try harder.
    if not any(DATE_TOKEN_RE.search(page.markdown) for page in content_pages):
        return True
    return False


def _is_event_source(source: "Source") -> bool:
    """Event-leaning sources keep escalating even when their feed looks fresh."""
    if source.events_url:
        return True
    text = f"{source.category or ''} {source.type or ''}".lower()
    return any(
        word in text
        for word in (
            "venue", "event", "museum", "library", "park", "theatre",
            "theater", "music", "arts", "festival", "gallery", "center",
        )
    )


def _is_recent(value: Optional[str], days: int) -> bool:
    parsed = _feed_dt(value)
    if parsed is None:
        return False
    return (utc_now() - parsed) <= timedelta(days=days)


def _needs_curl_fallback(pages: list[PageResult], errors: list[str]) -> bool:
    """crawl4ai's HTTP client is blocked/empty enough to retry with curl_cffi."""
    if not pages:
        return True
    if not any(p.success and p.markdown.strip() for p in pages):
        return True
    return all((p.status_code or 0) >= 400 for p in pages)


def _has_dated_content(pages: list[PageResult]) -> bool:
    content = [
        p for p in pages if p.kind == "page" and p.success and p.markdown.strip()
    ]
    if not content:
        return False
    words = sum(len(p.markdown.split()) for p in content)
    return words >= 500 and any(DATE_TOKEN_RE.search(p.markdown) for p in content)


def _as_result_list(raw: Any) -> list[Any]:
    if isinstance(raw, (list, tuple)):
        return list(raw)
    results = getattr(raw, "results", None)
    if isinstance(results, list) and results:
        return list(results)
    return [raw] if raw is not None else []


def _to_page(result: Any, fallback_url: str) -> PageResult:
    status = getattr(result, "status_code", None)
    success = bool(getattr(result, "success", False)) and status is not None and status < 400
    error = getattr(result, "error_message", None) or None
    if not success and not error:
        error = f"HTTP {status}" if status else "crawl failed"

    markdown = _raw_markdown(getattr(result, "markdown", None))
    metadata = getattr(result, "metadata", None) or {}
    depth = metadata.get("depth") if isinstance(metadata, dict) else None
    if not isinstance(depth, int):
        dispatch = getattr(result, "dispatch_result", None)
        depth = getattr(dispatch, "depth", None) if dispatch is not None else None

    page_html = getattr(result, "html", None)
    page_url = getattr(result, "url", None) or fallback_url
    return PageResult(
        url=page_url,
        final_url=getattr(result, "redirected_url", None) or getattr(result, "url", None),
        status_code=status,
        title=_extract_title(metadata, markdown),
        markdown=markdown,
        html=page_html,
        images=_extract_images(page_html or "", page_url),
        depth=depth,
        success=success,
        error=None if success else error,
    )


def _raw_markdown(markdown: Any) -> str:
    if markdown is None:
        return ""
    if isinstance(markdown, str):
        return markdown
    return getattr(markdown, "raw_markdown", "") or ""


def _extract_title(metadata: dict[str, Any], markdown: str) -> Optional[str]:
    for key in ("title", "og:title"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


_IMAGE_META_RES = (
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<link[^>]+rel=["\']image_src["\'][^>]*?href=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
)


def _extract_images(html_text: str, base_url: str) -> list[str]:
    """Canonical page image(s): og:image / twitter:image / rel=image_src.

    Falls back to the first plausible content <img> when no social image is
    declared, so we are more likely to have something to attach to a find.
    """
    found: list[str] = []
    for pattern in _IMAGE_META_RES:
        for match in pattern.finditer(html_text or ""):
            value = html.unescape(match.group(1)).strip()
            if value:
                found.append(urljoin(base_url, value))
    if found:
        return _dedupe_strings(found)

    for match in re.finditer(r"<img\b[^>]*>", html_text or "", re.IGNORECASE):
        tag = match.group(0)
        for attr in re.finditer(
            r"\b(?:src|data-src|data-lazy-src|data-original)=[\"']([^\"']+)", tag, re.I
        ):
            candidate = urljoin(base_url, html.unescape(attr.group(1)).strip())
            if _is_content_image(candidate, tag):
                found.append(candidate)
                break
        if len(found) < 3:
            srcset = re.search(r"\bsrcset=[\"']([^\"']+)", tag, re.I)
            if srcset:
                first = srcset.group(1).split(",")[0].strip().split(" ")[0]
                candidate = urljoin(base_url, first)
                if _is_content_image(candidate, tag):
                    found.append(candidate)
        if len(found) >= 3:
            break

    if not found:
        # Some CMSes (e.g. Finalsite) carry image URLs in a data attribute.
        for match in re.finditer(
            r'data-image-sizes=["\']([^"\']+)["\']', html_text or "", re.I
        ):
            try:
                data = json.loads(html.unescape(unquote(match.group(1))))
            except Exception:  # noqa: BLE001
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and item.get("url"):
                    found.append(str(item["url"]))
            if len(found) >= 3:
                break
    return _dedupe_strings(found)


def _is_content_image(url: str, tag: str) -> bool:
    low = url.lower()
    if low.startswith("data:") or low.endswith(".svg"):
        return False
    if any(
        key in low
        for key in (
            "icon", "logo", "sprite", "favicon", "avatar", "placeholder",
            "spacer", "pixel", "blank.gif", "/theme/", "/themes/", "1x1",
        )
    ):
        return False
    for attr in ("width", "height"):
        dim = re.search(rf"\b{attr}=[\"']?(\d+)", tag, re.I)
        if dim and int(dim.group(1)) < 100:
            return False
    return True


def crawl4ai_version() -> str:
    try:
        from crawl4ai.__version__ import __version__

        return f"crawl4ai {__version__}"
    except Exception:  # noqa: BLE001
        return "crawl4ai"


# --------------------------------------------------------------------------- #
# non-HTML asset harvesting
# --------------------------------------------------------------------------- #
def _browser_seeds_for(source: "Source") -> list[str]:
    """Main URL, dedicated events URL, and any explicit crawl_urls."""
    seeds = [source.url]
    known = {_normalize_url(source.url)}
    for extra in (source.events_url, *source.crawl_urls):
        if extra and _normalize_url(extra) not in known:
            known.add(_normalize_url(extra))
            seeds.append(extra)
    return seeds


def _feed_urls_for(source: "Source") -> list[str]:
    """Explicit feed URLs plus any seed that is itself a feed/asset URL."""
    feeds = list(source.feed_urls)
    if source.events_url and _extension(source.events_url) in ASSET_EXTENSIONS:
        feeds.append(source.events_url)
    return _dedupe_strings(feeds)


def _extension(url: str) -> str:
    return os.path.splitext(urlparse(url).path)[1].lower()


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _normalize_url(url: str) -> str:
    url = (url or "").split("#", 1)[0].rstrip("/")
    if "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme.lower()}://{rest.lower()}"
    return url.lower()


def _dedupe_pages(pages: list[PageResult]) -> list[PageResult]:
    """Drop repeated URLs and identical bodies, keeping the most useful page.

    A URL can appear more than once with different outcomes (e.g. a failed
    crawl4ai-HTTP page followed by a successful curl_cffi page); we must keep
    the successful/bigger one, not merely the first.
    """
    best: dict[str, PageResult] = {}
    order: list[str] = []
    seen_hashes: set[str] = set()
    for page in pages:
        url_key = _normalize_url(page.final_url or page.url)
        digest = _sha256(page.markdown) if page.markdown.strip() else None
        if digest and digest in seen_hashes:
            continue
        existing = best.get(url_key)
        if existing is None:
            best[url_key] = page
            order.append(url_key)
            if digest:
                seen_hashes.add(digest)
        elif _page_score(page) > _page_score(existing):
            if digest:
                seen_hashes.add(digest)
            best[url_key] = page
    return [best[key] for key in order]


def _page_score(page: PageResult) -> tuple[int, int]:
    return (1 if page.success and page.markdown.strip() else 0, len(page.markdown))


def _is_ical_url(url: str) -> bool:
    """True for ICS calendar links: ``.ics`` files, ``webcal://`` feeds, and
    the query-style per-event calendars that Squarespace (``?format=ical``)
    and The Events Calendar (``?ical=1``) link from every event page."""
    if not url:
        return False
    lowered = url.lower()
    if lowered.startswith("webcal://"):
        return True
    if _extension(url) == ".ics":
        return True
    return bool(_ICAL_QUERY_RE.search(urlparse(url).query))


def _webcal_to_http(url: str) -> str:
    """``webcal://`` is ICS over HTTP(S); fetchers reject the raw scheme."""
    lowered = url.lower()
    if lowered.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def _harvest_asset_urls(pages: list[PageResult]) -> list[str]:
    """Find linked pdf/ics/xml/rss/atom/json URLs in crawled markdown.

    ICS calendars are also matched by query (``?format=ical``, ``?ical=1``) or
    ``webcal://`` scheme, because Squarespace and The Events Calendar expose
    per-event iCal feeds that way instead of as ``.ics`` files."""
    found: list[str] = []
    for page in pages:
        if not page.markdown:
            continue
        for match in ASSET_URL_RE.finditer(page.markdown):
            url = match.group(0).rstrip(".,;:)]}")
            if _extension(url) in ASSET_EXTENSIONS or _is_ical_url(url):
                found.append(_webcal_to_http(url))
    return _dedupe_strings(found)


def _http_get(url: str, timeout: int) -> tuple[int, str, str, bytes]:
    """GET over HTTP, preferring curl_cffi's browser TLS impersonation.

    Returns (status, content_type, charset, body). HTTP >= 400 is returned as a
    status (not raised) so callers can log it.
    """
    if _curl_requests is not None:
        response = _curl_requests.get(
            url, impersonate="chrome", timeout=timeout, allow_redirects=True
        )
        ctype = (response.headers.get("Content-Type") or "").strip()
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", ctype, re.IGNORECASE)
        if match:
            charset = match.group(1)
        return response.status_code, ctype.split(";", 1)[0].strip().lower(), charset, response.content

    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            data = response.read(MAX_ASSET_BYTES)
    except urllib.error.HTTPError as exc:
        headers = exc.headers
        return (
            exc.code,
            (headers.get_content_type() if headers else ""),
            "utf-8",
            exc.read(MAX_ASSET_BYTES),
        )
    if "gzip" in encoding:
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    elif "deflate" in encoding:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
    return status, content_type, charset, data


def _still_failing(pages: list[PageResult]) -> bool:
    """True when there is no usable dated content, for the API last resort."""
    content = [p for p in pages if p.success and p.markdown.strip()]
    if not content:
        return True
    return not any(DATE_TOKEN_RE.search(p.markdown) for p in content)


def _mask_secret(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _build_api_url(
    url: str, provider: str, key: str, template: str, render: bool
) -> str:
    if provider == "generic":
        return template.format(url=quote(url, safe=""), key=key) if template else ""
    quoted = quote(url, safe="")
    flag = "true" if render else "false"
    if provider == "scraperapi":
        return f"https://api.scraperapi.com/?api_key={key}&url={quoted}&render={flag}"
    if provider == "scrapingbee":
        return f"https://app.scrapingbee.com/api/v1/?api_key={key}&url={quoted}&render_js={flag}"
    if provider == "scrapingant":
        return f"https://api.scrapingant.com/v2/general?x-api-key={key}&url={quoted}&browser={flag}"
    return ""


def _fetch_via_api(
    url: str,
    timeout: int,
    provider: str,
    key: str,
    template: str,
    render: bool,
) -> PageResult:
    """Fetch a URL through a commercial scraping API and render it uniformly."""
    target = _build_api_url(url, provider, key, template, render)
    title = os.path.basename(urlparse(url).path) or url
    if not target:
        return _asset_failure(url, title, None, "api: no provider url", "page")
    try:
        status, content_type, charset, data = _http_get(target, timeout)
    except Exception as exc:  # noqa: BLE001
        return _asset_failure(
            url, title, None, _mask_secret(f"api: {type(exc).__name__}: {exc}", key), "page"
        )
    if status >= 400:
        return _asset_failure(
            url, title, status, _mask_secret(f"api HTTP {status}: {data[:200]!r}", key), "page"
        )
    text = _decode(data, charset)
    markdown = _parse_html(text, url)
    return PageResult(
        url=url,
        final_url=url,
        status_code=status,
        title=title,
        markdown=markdown,
        html=text,
        depth=None,
        success=bool(markdown.strip()),
        error=None if markdown.strip() else "api: empty",
        kind="page",
    )


def _fetch_resource(url: str, timeout: int) -> PageResult:
    """Fetch any URL over plain HTTP and convert it to text."""
    title = os.path.basename(urlparse(url).path) or url
    if not url.startswith(("http://", "https://")):
        return _asset_failure(url, title, None, f"unsupported scheme: {url[:60]}", "page")
    try:
        status, content_type, charset, data = _http_get(url, timeout)
    except Exception as exc:  # noqa: BLE001 - surfaced to the log
        return _asset_failure(url, title, None, f"{type(exc).__name__}: {exc}", "page")
    if status >= 400:
        return _asset_failure(url, title, status, f"HTTP {status}", "page")

    ext = _extension(url)
    html_text: Optional[str] = None
    feed_meta: Optional[dict[str, Any]] = None
    images: list[str] = []
    kind = "asset"
    try:
        if (ext == ".pdf" or content_type == "application/pdf") and "html" not in content_type:
            markdown = _parse_pdf(data, url)
        else:
            text = _decode(data, charset)
            head = text.lstrip()[:512].lower()
            # R3: classify feeds by body, not content-type/extension.
            looks_xml = (
                head.startswith("<?xml")
                or "<rss" in head
                or "<feed" in head
                or "<rdf" in head
            )
            if ext == ".ics" or _is_ical_url(url) or "calendar" in content_type:
                markdown = _parse_ics(text, url)
            elif (
                ext in (".xml", ".rss", ".atom") or "xml" in content_type or looks_xml
            ) and "<html" not in head and "<!doctype" not in head:
                markdown, feed_meta = _parse_feed(text, url)
                feed_meta["status_code"] = status
                images = list(feed_meta.get("images") or [])
            elif ext == ".json" or "json" in content_type:
                markdown = f"# JSON: {url}\n\n```json\n{text}\n```"
            elif _is_instagram(url):
                # Public IG profiles embed recent post captions (with dates and
                # ticket links) in the server-rendered HTML, no login needed.
                markdown, feed_meta = _parse_instagram(text, url)
                feed_meta["status_code"] = status
                images = list(feed_meta.get("images") or [])
            else:
                html_text = text
                images = _extract_images(html_text, url)
                markdown = _parse_html(html_text, url)
                kind = "page"
    except Exception as exc:  # noqa: BLE001 - keep the crawl going
        return _asset_failure(
            url, title, status, f"parse error: {type(exc).__name__}: {exc}", kind
        )

    return PageResult(
        url=url,
        final_url=url,
        status_code=status,
        title=title,
        markdown=markdown,
        html=html_text,
        images=images,
        depth=None,
        success=bool(markdown.strip()),
        error=None if markdown.strip() else "empty resource",
        kind=kind,
        feed=feed_meta,
    )


def _asset_failure(
    url: str, title: str, status: Optional[int], error: str, kind: str = "asset"
) -> PageResult:
    return PageResult(
        url=url,
        final_url=url,
        status_code=status,
        title=title,
        markdown="",
        html=None,
        depth=None,
        success=False,
        error=error,
        kind=kind,
    )


def _parse_html(html: str, url: str) -> str:
    """Render raw HTML through crawl4ai's own pipeline (no browser).

    This is what makes HTTP-fetched HTML, feed pages, and browser-rendered
    pages all produce the same markdown shape.
    """
    from crawl4ai import DefaultMarkdownGenerator, WebScrapingStrategy

    scraped = WebScrapingStrategy().scrap(url, html)
    cleaned = getattr(scraped, "cleaned_html", "") or html
    result = DefaultMarkdownGenerator().generate_markdown(cleaned, base_url=url)
    return getattr(result, "raw_markdown", "") or ""


def _discover_feeds(html: str, base_url: str) -> list[str]:
    """RSS/Atom feeds advertised via <link rel=alternate> in <head>.

    Filtered to site-level feeds: WordPress advertises a per-post feed on every
    article page, which are noise (and usually 410).
    """
    found: list[str] = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.IGNORECASE):
        if re.search(r"type=[\"']application/(?:rss|atom)\+xml", tag, re.IGNORECASE):
            href = re.search(r"href=[\"']([^\"']+)", tag, re.IGNORECASE)
            if href:
                found.append(urljoin(base_url, href.group(1)))
    return _site_level_feeds(_dedupe_strings(found))


#: Terminal path tokens that identify a feed endpoint.
_FEED_TAILS = {"feed", "rss", "atom", "rss.xml", "atom.xml", "feed.xml", "index.xml"}
#: Single-segment prefixes that are never useful feeds.
_FEED_BLOCKED_PREFIX = {"comments", "comment", "trackback"}


def _is_site_level_feed(url: str) -> bool:
    """True for a site/section feed, False for a per-article or junk feed.

    Keeps ``/feed``, ``/feed/atom``, ``/rss``, ``/blog/feed``, ``/news/feed``;
    rejects ``/113459/features/2026/09/…/feed`` (≥2 path segments before the feed
    token), ``/comments/feed``, and page-slug prefixes (numeric, ≥2 hyphens, or
    long).
    """
    parts = urlparse(url)
    segments = [s for s in parts.path.split("/") if s]
    if segments and (segments[-1].lower() in _FEED_TAILS
                     or segments[-1].lower().endswith((".rss", ".atom"))):
        prefix = segments[:-1]
    elif "feed=" in parts.query.lower() or "format=rss" in parts.query.lower() \
            or "format=atom" in parts.query.lower():
        prefix = segments
    else:
        return False
    if len(prefix) > 1:
        return False
    if prefix:
        seg = prefix[0].lower()
        if seg in _FEED_BLOCKED_PREFIX:
            return False
        if re.search(r"\d", seg) or seg.count("-") >= 2 or len(seg) > 20:
            return False
    return True


def _site_level_feeds(urls: list[str], per_host: int = 3) -> list[str]:
    """Keep site-level feeds only, preferring short paths, capped per host."""
    counts: dict[str, int] = {}
    out: list[str] = []
    for url in sorted(urls, key=lambda u: len(urlparse(u).path)):
        if not _is_site_level_feed(url):
            continue
        host = urlparse(url).netloc.lower()
        if counts.get(host, 0) >= per_host:
            continue
        counts[host] = counts.get(host, 0) + 1
        out.append(url)
    return out


def _discover_feeds_in_pages(pages: list[PageResult]) -> list[str]:
    """Discover feeds from the seed/home pages only (depth 0).

    Running this over every crawled page is what pulled in per-article feeds.
    """
    found: list[str] = []
    for page in pages:
        if page.kind == "page" and page.html and page.depth == 0:
            found.extend(_discover_feeds(page.html, page.final_url or page.url))
    return _site_level_feeds(_dedupe_strings(found))


_JSONLD_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_pages(pages: list[PageResult]) -> list[PageResult]:
    """Extract schema.org JSON-LD blocks from collected HTML as extra pages."""
    extra: list[PageResult] = []
    for page in list(pages):
        if page.kind != "page" or not page.html:
            continue
        blocks: list[str] = []
        for match in _JSONLD_RE.finditer(page.html):
            try:
                data = json.loads(match.group(1).strip())
            except Exception:  # noqa: BLE001 - tolerate malformed JSON-LD
                continue
            for obj in data if isinstance(data, list) else [data]:
                if isinstance(obj, dict) and obj.get("@type"):
                    blocks.append(
                        json.dumps(obj, indent=2, ensure_ascii=False)[:20000]
                    )
        if not blocks:
            continue
        markdown = (
            f"# Structured data (JSON-LD): {page.final_url or page.url}\n\n"
            + "\n\n".join(f"```json\n{block}\n```" for block in blocks)
        )
        extra.append(
            PageResult(
                url=(page.final_url or page.url) + "#jsonld",
                final_url=page.final_url,
                status_code=page.status_code,
                title=f"JSON-LD: {page.title or page.url}",
                markdown=markdown,
                html=None,
                depth=page.depth,
                success=True,
                error=None,
                kind="jsonld",
            )
        )
    return extra


def _decode(data: bytes, charset: str) -> str:
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _parse_ics(text: str, url: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    events: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";", 1)[0].upper()] = value

    out = [f"# Calendar feed (ICS): {url}", ""]
    for event in events:
        summary = _unescape_ics(event.get("SUMMARY", "(untitled)"))
        start = _format_ics_datetime(event.get("DTSTART", ""))
        end = _format_ics_datetime(event.get("DTEND", ""))
        when = start + (f" to {end}" if end and end != start else "")
        entry = f"- {when}: {summary}"
        location = _unescape_ics(event.get("LOCATION", ""))
        if location:
            entry += f" @ {location}"
        description = _unescape_ics(event.get("DESCRIPTION", ""))
        if description:
            entry += f" -- {re.sub(r'\\s+', ' ', description)[:300]}"
        out.append(entry)
        # Continuation lines (same shape as the RSS render): the event's own
        # page when the feed states it, and the UID (The Events Calendar UIDs
        # are ``post_id-start-end@host`` and resolve to the event page).
        uid = (event.get("UID") or "").strip()
        if uid:
            out.append(f"  uid: {uid}")
        url_value = (event.get("URL") or "").strip()
        if url_value:
            out.append(f"  url: {url_value}")
    if len(out) == 2:
        out.append("(no VEVENT entries found)")
    return "\n".join(out)


def _unescape_ics(value: str) -> str:
    return html.unescape(
        value.replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\\\", "\\")
        .strip()
    )


def _format_ics_datetime(value: str) -> str:
    value = value.strip()
    if not value:
        return "(no date)"
    if value.endswith("Z"):
        core, suffix = value[:-1], "Z"
    else:
        core, suffix = value, ""
    if "T" in core:
        date_part, time_part = core.split("T", 1)
        time_part = time_part[:6]
        if len(date_part) == 8 and len(time_part) >= 4:
            return (
                f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} "
                f"{time_part[0:2]}:{time_part[2:4]}{suffix}"
            )
    if len(core) == 8:
        return f"{core[0:4]}-{core[4:6]}-{core[6:8]}"
    return value


def _parse_pdf(data: bytes, url: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = [f"# PDF: {url}", f"({len(reader.pages)} pages)", ""]
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page shouldn't stop us
            text = f"[page {number} extraction failed: {type(exc).__name__}: {exc}]"
        if text.strip():
            parts.append(f"## Page {number}")
            parts.append(text.strip())
    return "\n\n".join(parts)


NS_FEED = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\r", " ").replace("\n", " ")).strip()


def _feed_dt(value: Optional[str]) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(value)
        except Exception:  # noqa: BLE001
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_feed_date(value: Optional[str]) -> str:
    parsed = _feed_dt(value)
    return parsed.isoformat(timespec="seconds") if parsed else (value or "").strip()


def _is_instagram(url: str) -> bool:
    return "instagram.com" in (urlparse(url).hostname or "")


def _parse_instagram(html_text: str, url: str) -> tuple[str, dict[str, Any]]:
    """Extract recent post captions from a public Instagram profile page."""
    entries: list[str] = []
    for match in re.finditer(
        r'"caption"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"', html_text
    ):
        entries.append(_json_unescape(match.group(1)))
    if not entries:  # looser fallback if the shape changes
        for match in re.finditer(
            r'"text"\s*:\s*"((?:[^"\\]|\\.){40,})"', html_text
        ):
            value = _json_unescape(match.group(1))
            if value and not value.startswith("http"):
                entries.append(value)

    captions = _dedupe_strings(
        [re.sub(r"\s+", " ", entry).strip() for entry in entries if entry.strip()]
    )
    links = _dedupe_strings(
        re.findall(r"https?://[^\s)\"'<>]+", " ".join(captions))
    )
    bio = ""
    match = re.search(r'<meta property="og:description" content="([^"]*)"', html_text)
    if match:
        bio = html.unescape(match.group(1)).strip()

    lines = [f"# Instagram: {url}   ({len(captions)} recent posts)", ""]
    if bio:
        lines += [bio, ""]
    lines += [f"- {caption}" for caption in captions]
    if not captions:
        lines.append("(no captions extracted)")

    images = _dedupe_strings(
        [
            value
            for value in (
                _json_unescape(m.group(1))
                for m in re.finditer(
                    r'"display_(?:uri|url)"\s*:\s*"((?:[^"\\]|\\.)*)"', html_text
                )
            )
            if value.startswith("http")
        ]
    )
    summary = {
        "url": url,
        "kind": "instagram",
        "items": len(captions),
        "newest": "",
        "oldest": "",
        "links": links[:FEED_LINKS_IN_META],
        "articles": [],
        "images": images[:FEED_LINKS_IN_META],
        "media": {"article": len(captions)},
    }
    return "\n".join(lines), summary


def _json_unescape(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except Exception:  # noqa: BLE001
        return value


def _parse_feed(text: str, url: str) -> tuple[str, dict[str, Any]]:
    """Parse RSS/Atom into (markdown, summary).

    R1: keep the fields the XML already carries (author, categories, image,
    media kind, fuller summary) instead of discarding them. The summary is
    mirrored into meta.json for downstream joins (R5).
    """
    root = ET.fromstring(text)
    is_atom = root.tag.endswith("}feed") or root.tag == "feed"
    items = root.findall(".//item") or root.findall(".//atom:entry", NS_FEED)

    entries: list[dict[str, Any]] = []
    for item in items:
        title = (
            item.findtext("title")
            or item.findtext("atom:title", namespaces=NS_FEED)
            or ""
        ).strip()

        if is_atom:
            link = ""
            for link_el in item.findall("atom:link", NS_FEED):
                if link_el.get("rel", "alternate") in ("alternate", ""):
                    link = link_el.get("href", "")
                    break
            if not link:
                link_el = item.find("atom:link", NS_FEED)
                link = link_el.get("href", "") if link_el is not None else ""
        else:
            link = (item.findtext("link") or "").strip()
            if not link:
                guid = item.find("guid")
                if guid is not None and guid.get("isPermaLink", "true").lower() == "true":
                    link = (guid.text or "").strip()
        # Feeds often carry relative or scheme-relative links.
        link = urljoin(url, link) if link else ""

        raw_date = (
            item.findtext("pubDate")
            or item.findtext("dc:date", namespaces=NS_FEED)
            or item.findtext("atom:published", namespaces=NS_FEED)
            or item.findtext("atom:updated", namespaces=NS_FEED)
            or ""
        )

        author = (
            item.findtext("dc:creator", namespaces=NS_FEED)
            or item.findtext("author")
            or ""
        ).strip()
        if not author:
            author_el = item.find("atom:author/atom:name", NS_FEED)
            if author_el is not None:
                author = (author_el.text or "").strip()

        categories: list[str] = []
        for cat in item.findall("category") + item.findall("atom:category", NS_FEED):
            value = (cat.get("term") or cat.text or "").strip()
            if value:
                categories.append(value)

        raw_body = (
            item.findtext("content:encoded", namespaces=NS_FEED)
            or item.findtext("description")
            or item.findtext("atom:content", namespaces=NS_FEED)
            or item.findtext("atom:summary", namespaces=NS_FEED)
            or ""
        )
        body = _strip_tags(raw_body)[:FEED_SUMMARY_MAX]

        image = ""
        media_els = item.findall("media:content", NS_FEED) + item.findall(
            "media:thumbnail", NS_FEED
        )
        for media_el in media_els:
            if media_el.get("url"):
                image = media_el.get("url", "")
                break

        media_type = ""
        medium = ""
        enclosure = item.find("enclosure")
        if enclosure is not None:
            media_type = enclosure.get("type", "")
            if not image and media_type.startswith("image"):
                image = enclosure.get("url", "")
        if not media_type and media_els:
            media_type = media_els[0].get("type", "")
            medium = media_els[0].get("medium", "")
        if not image:
            match = re.search(r'<img[^>]+src=["\']([^"\']+)', raw_body or "", re.I)
            if match:
                image = match.group(1)
        image = urljoin(url, image) if image else ""
        # A lead image does not make the item an image post; only a real
        # audio/video enclosure is non-article media (R6).
        if media_type.startswith("audio") or medium == "audio":
            media_kind = "audio"
        elif media_type.startswith("video") or medium == "video":
            media_kind = "video"
        else:
            media_kind = "article"

        entries.append(
            {
                "title": title,
                "link": link,
                "date": _normalize_feed_date(raw_date),
                "author": _one_line(author),
                "categories": categories,
                "image": image,
                "media": media_kind,
                "summary": body,
            }
        )

    kind = "atom" if is_atom else "rss"
    dated = sorted(
        ((_feed_dt(entry["date"]), entry["date"]) for entry in entries if entry["date"]),
        key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC),
    )
    dated = [(dt, value) for dt, value in dated if dt is not None]
    newest = dated[-1][1] if dated else ""
    oldest = dated[0][1] if dated else ""

    lines = [
        f"# Feed: {url}   ({kind}; {len(entries)} items; latest {newest or 'n/a'})",
        "",
    ]
    for entry in entries:
        lines.append(
            f"- {entry['date'] or '(no date)'} | {entry['title']} | {entry['link']}".rstrip()
        )
        if entry["author"]:
            lines.append(f"  author: {entry['author']}")
        if entry["categories"]:
            lines.append(f"  categories: {'; '.join(entry['categories'])}")
        if entry["image"]:
            lines.append(f"  image: {entry['image']}")
        lines.append(f"  media: {entry['media']}")
        if entry["summary"]:
            lines.append(f"  summary: {entry['summary']}")
    if not entries:
        lines.append("(no feed entries found)")

    links = _dedupe_strings(
        entry["link"] for entry in entries if entry["link"].startswith(("http://", "https://"))
    )
    articles = _dedupe_strings(
        entry["link"]
        for entry in entries
        if entry["link"].startswith(("http://", "https://")) and entry["media"] == "article"
    )
    media_counts = Counter(entry["media"] for entry in entries)
    images = _dedupe_strings([entry["image"] for entry in entries if entry["image"]])
    summary = {
        "url": url,
        "kind": kind,
        "items": len(entries),
        "newest": newest,
        "oldest": oldest,
        "links": links[:FEED_LINKS_IN_META],
        "articles": articles[:FEED_LINKS_IN_META],
        "images": images[:FEED_LINKS_IN_META],
        "media": dict(media_counts),
    }
    return "\n".join(lines), summary


# --------------------------------------------------------------------------- #
# writing results
# --------------------------------------------------------------------------- #
_KIND_ORDER = {"page": 0, "asset": 1, "jsonld": 2}


def _ordered_pages(pages: list[PageResult]) -> list[PageResult]:
    return sorted(
        pages,
        key=lambda page: (
            _KIND_ORDER.get(page.kind, 3),
            page.depth if isinstance(page.depth, int) else 0,
        ),
    )


def combine_pages(pages: list[PageResult]) -> str:
    """Concatenate every crawled page and asset's raw text into one document."""
    blocks: list[str] = []
    for index, page in enumerate(_ordered_pages(pages), start=1):
        label = page.kind
        title = page.title or page.final_url or page.url
        block = [
            f"<!-- {label} {index} -->",
            f"# {title}",
            f"URL: {page.final_url or page.url}",
        ]
        if page.status_code is not None:
            block.append(f"Status: {page.status_code}")
        if page.error:
            block.append(f"Error: {page.error}")
        block.append("")
        block.append(page.markdown.strip())
        blocks.append("\n".join(block).strip())
    return "\n\n---\n\n".join(blocks) + "\n"


def write_result(
    output_dir: Path,
    source: Source,
    outcome: CrawlOutcome,
    *,
    fetched_at: datetime,
    backend_name: str,
    settings: dict[str, Any],
    save_html: bool,
) -> tuple[Path, int, int, int]:
    source_dir = output_dir / slugify(source.name)
    source_dir.mkdir(parents=True, exist_ok=True)

    content = combine_pages(outcome.pages)
    word_count = len(content.split())
    write_atomic(source_dir / "content.md", content)

    if save_html:
        html_dir = source_dir / "html"
        if html_dir.exists():
            for stale in html_dir.glob("*.html"):
                stale.unlink()
        html_dir.mkdir(parents=True, exist_ok=True)
        number = 0
        for page in _ordered_pages(outcome.pages):
            if page.kind == "page" and page.html:
                number += 1
                write_atomic(html_dir / f"{number:03d}.html", page.html)

    page_meta = [
        {
            "index": index,
            "kind": page.kind,
            "url": page.url,
            "final_url": page.final_url,
            "status_code": page.status_code,
            "title": page.title,
            "image": page.images[0] if page.images else None,
            "images": page.images[:5],
            "depth": page.depth,
            "success": page.success,
            "error": page.error,
            "chars": len(page.markdown),
            "bytes": len(page.markdown.encode("utf-8")),
            "sha256": _sha256(page.markdown),
        }
        for index, page in enumerate(_ordered_pages(outcome.pages), start=1)
    ]

    primary_image = ""
    for page in _ordered_pages(outcome.pages):
        if page.images:
            primary_image = page.images[0]
            break
    if not primary_image:
        for summary in outcome.feeds:
            feed_images = summary.get("images") or []
            if feed_images:
                primary_image = feed_images[0]
                break

    meta = {
        "source": source.to_meta(),
        "crawl": {
            "primary_image": primary_image,
            "seeds": outcome.seeds or [source.url],
            "seed_url": source.url,
            "seed_final_url": outcome.seed_final_url,
            "seed_status_code": outcome.seed_status_code,
            "seed_title": outcome.seed_title,
            "fetched_at": to_iso(fetched_at),
            "backend": backend_name,
            "settings": settings,
            "pages_crawled": sum(1 for p in outcome.pages if p.kind == "page"),
            "pages_failed": outcome.pages_failed,
            "assets_fetched": outcome.assets_fetched,
            "browser_used": outcome.browser_used,
            "api_used": outcome.api_used,
            "auto_feeds": outcome.auto_feeds,
            "feeds": outcome.feeds,
            "jsonld_blocks": sum(1 for p in outcome.pages if p.kind == "jsonld"),
            "pages": page_meta,
        },
        "content": {
            "file": "content.md",
            "chars": len(content),
            "bytes": len(content.encode("utf-8")),
            "sha256": _sha256(content),
            "word_count": word_count,
        },
    }
    write_atomic(source_dir / "meta.json", json.dumps(meta, indent=2) + "\n")

    return (
        source_dir,
        len(content.encode("utf-8")),
        sum(1 for p in outcome.pages if p.kind == "page"),
        word_count,
    )


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# run orchestration
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    source: Source
    trigger: str


async def crawl_job(
    job: Job,
    backend: Crawl4AIBackend,
    conn: sqlite3.Connection,
    output_dir: Path,
    semaphore: asyncio.Semaphore,
    settings: dict[str, Any],
    save_html: bool,
    on_done: Callable[[RunRecord], None],
) -> RunRecord:
    source = job.source
    async with semaphore:
        started = utc_now()
        start_perf = time.perf_counter()
        outcome = await backend.fetch(source)
        duration_ms = int((time.perf_counter() - start_perf) * 1000)

        record = RunRecord(
            source_name=source.name,
            source_url=source.url,
            trigger=job.trigger,
            started_at=started,
            finished_at=utc_now(),
            duration_ms=duration_ms,
            success=outcome.success,
            status_code=outcome.seed_status_code,
            final_url=outcome.seed_final_url,
            title=outcome.seed_title,
            pages_failed=outcome.pages_failed,
            assets_fetched=outcome.assets_fetched,
            backend=backend.name,
            error=outcome.error,
        )

        if outcome.success:
            source_dir, content_bytes, pages_crawled, word_count = write_result(
                output_dir,
                source,
                outcome,
                fetched_at=record.finished_at,
                backend_name=backend.name,
                settings=settings,
                save_html=save_html,
            )
            record.content_bytes = content_bytes
            record.pages_crawled = pages_crawled
            record.word_count = word_count
            record.output_dir = str(source_dir)

        insert_run(conn, record)
        on_done(record)
        return record


async def run_jobs(
    jobs: list[Job],
    backend: Crawl4AIBackend,
    conn: sqlite3.Connection,
    output_dir: Path,
    concurrency: int,
    settings: dict[str, Any],
    save_html: bool,
) -> list[RunRecord]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    total = len(jobs)
    completed = 0
    records: list[RunRecord] = []

    def report(record: RunRecord) -> None:
        nonlocal completed
        completed += 1
        mark = "ok  " if record.success else "FAIL"
        if record.success:
            detail = (
                f"{human_bytes(record.content_bytes)}, "
                f"{record.pages_crawled} pages, "
                f"{record.assets_fetched or 0} assets, "
                f"{record.word_count or 0} words"
            )
        else:
            detail = record.error or "unknown error"
        print(
            f"[{completed:>{len(str(total))}}/{total}] {mark} "
            f"{record.source_name} ({record.duration_ms / 1000:.1f}s) - {detail}",
            flush=True,
        )

    tasks = [
        asyncio.create_task(
            crawl_job(
                job, backend, conn, output_dir, semaphore, settings, save_html, report
            )
        )
        for job in jobs
    ]
    for task in asyncio.as_completed(tasks):
        records.append(await task)
    return records


def select_jobs(
    sources: list[Source],
    *,
    force: bool,
    successes: dict[str, datetime],
    now: datetime,
    limit: Optional[int],
) -> tuple[list[Job], list[tuple[Source, datetime]]]:
    jobs: list[Job] = []
    skipped: list[tuple[Source, datetime]] = []
    for source in sources:
        if force:
            jobs.append(Job(source, "force"))
            continue
        last_ok = successes.get(source.name)
        due_at = last_ok + timedelta(seconds=source.period_seconds) if last_ok else None
        if due_at is None or now >= due_at:
            jobs.append(Job(source, "scheduled"))
        else:
            skipped.append((source, due_at))
    if limit is not None:
        jobs = jobs[:limit]
    return jobs, skipped


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def print_status(
    sources: list[Source],
    latest: dict[str, sqlite3.Row],
    successes: dict[str, datetime],
    now: datetime,
) -> None:
    headers = ["STATUS", "SOURCE", "FREQ", "LAST RUN", "LAST OK", "NEXT DUE", "SIZE"]
    rows: list[list[str]] = []
    for source in sources:
        run = latest.get(source.name)
        last_ok = successes.get(source.name)
        if run is None:
            status, last_run, size = "never", "-", "-"
        else:
            status = "ok" if run["success"] else "fail"
            last_run = human_delta(parse_iso(run["started_at"]), now)
            size = human_bytes(run["content_bytes"])

        if last_ok is None:
            next_due = "due now"
        else:
            due = last_ok + timedelta(seconds=source.period_seconds)
            next_due = human_until(due, now)

        rows.append(
            [
                status,
                _truncate(source.name, 48),
                source.frequency,
                last_run,
                human_delta(last_ok, now) if last_ok else "never",
                next_due,
                size,
            ]
        )
    _print_table(headers, rows)
    print(
        f"\n{len(sources)} sources | "
        f"{sum(1 for r in latest.values() if r['success'])} last run ok | "
        f"{sum(1 for r in latest.values() if not r['success'])} last run failed | "
        f"{len(sources) - len(latest)} never run"
    )


def print_errors(latest: dict[str, sqlite3.Row], now: datetime) -> None:
    failed = sorted(
        (row for row in latest.values() if not row["success"]),
        key=lambda row: row["started_at"],
        reverse=True,
    )
    if not failed:
        print("No sources failed their most recent run.")
        return
    headers = ["SOURCE", "LAST ATTEMPT", "CODE", "ERROR"]
    rows = [
        [
            _truncate(row["source_name"], 46),
            human_delta(parse_iso(row["started_at"]), now),
            str(row["status_code"]) if row["status_code"] is not None else "-",
            _truncate(row["error"] or "unknown", 60),
        ]
        for row in failed
    ]
    _print_table(headers, rows)
    print(f"\n{len(failed)} source(s) failed their most recent run.")


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl_sources.py",
        description=(
            "Deep-crawl Daily Brief sources that are due, logging every attempt "
            "to SQLite and keeping only the latest good result on disk."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  crawl_sources.py                       crawl sources that are due\n"
            "  crawl_sources.py --force               crawl every source now\n"
            "  crawl_sources.py --dry-run             list what would be crawled\n"
            "  crawl_sources.py --status              show last run per source\n"
            "  crawl_sources.py --errors              show sources failing latest run\n"
            "  crawl_sources.py --max-depth 3 --max-pages 100 --force\n"
            "  crawl_sources.py --save-html --limit 3 --force\n"
            "  crawl_sources.py --prune 50            keep only 50 log rows/source\n"
        ),
    )
    parser.add_argument(
        "--sources", type=Path, default=DEFAULT_SOURCE_FILE,
        help=f"source catalog JSON (default: {DEFAULT_SOURCE_FILE.name})",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite run log (default: {DEFAULT_DB.name})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"directory for latest good results (default: {DEFAULT_OUTPUT_DIR.name}/)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="crawl every source regardless of its frequency",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="print the last run for every source and exit",
    )
    parser.add_argument(
        "--errors", action="store_true",
        help="print sources whose most recent run failed and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list the sources that would be crawled and exit",
    )
    parser.add_argument(
        "--prune", type=int, metavar="N",
        help="keep only the last N log rows per source, then exit",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N",
        help="crawl at most N due sources (useful for testing)",
    )
    parser.add_argument(
        "--max-depth", type=int, default=3, metavar="N",
        help="link depth from each HTML seed URL (default: 3; 0 = seeds only)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=40, metavar="N",
        help="maximum HTML pages crawled per seed (default: 40)",
    )
    parser.add_argument(
        "--max-assets", type=int, default=30, metavar="N",
        help="maximum linked PDF/ICS/feed/JSON resources fetched per source "
        "(default: 30)",
    )
    parser.add_argument(
        "--no-assets", action="store_true",
        help="skip harvesting linked PDF/ICS/feed/JSON resources",
    )
    parser.add_argument(
        "--concurrency", type=int, default=3, metavar="N",
        help="sources crawled in parallel (default: 3)",
    )
    parser.add_argument(
        "--timeout", type=int, default=60, metavar="SECONDS",
        help="per-page/per-asset timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--retries", type=int, default=2, metavar="N",
        help="crawl4ai retries per page on failure (default: 2)",
    )
    parser.add_argument(
        "--save-html", action="store_true",
        help="also save each crawled page's raw HTML under <slug>/html/",
    )
    parser.add_argument(
        "--no-magic", action="store_true",
        help="disable crawl4ai 'magic' (no simulated user / overlay handling)",
    )
    parser.add_argument(
        "--no-stealth", action="store_true",
        help="disable the stealth browser profile",
    )
    parser.add_argument(
        "--api", choices=API_PROVIDERS,
        default=os.environ.get("SCRAPER_API_PROVIDER", "none"),
        help="commercial scraping API used only as a last resort "
        "(default: $SCRAPER_API_PROVIDER or none)",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("SCRAPER_API_KEY", ""),
        help="API key for --api (default: $SCRAPER_API_KEY)",
    )
    parser.add_argument(
        "--api-template", default=os.environ.get("SCRAPER_API_TEMPLATE", ""),
        help="URL template for --api generic, e.g. 'https://host/?key={key}&url={url}' "
        "(default: $SCRAPER_API_TEMPLATE)",
    )
    parser.add_argument(
        "--no-api-render", action="store_true",
        help="ask the scraping API not to render JavaScript (cheaper credits)",
    )
    parser.add_argument(
        "--api-concurrency", type=int, metavar="N",
        default=int(os.environ.get("SCRAPER_API_CONCURRENCY", "1") or "1"),
        help="parallel requests to the scraping API; many plans cap this at 1 "
        "(default: $SCRAPER_API_CONCURRENCY or 1)",
    )
    parser.add_argument(
        "--fail-on-error", action="store_true",
        help="exit non-zero if any crawl in this run failed",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_depth < 0:
        print("error: --max-depth must be >= 0", file=sys.stderr)
        return 2
    if args.max_pages < 1:
        print("error: --max-pages must be >= 1", file=sys.stderr)
        return 2
    if args.max_assets < 0:
        print("error: --max-assets must be >= 0", file=sys.stderr)
        return 2

    sources = load_sources(args.sources)
    conn = open_db(args.db)
    now = utc_now()

    try:
        if args.prune is not None:
            if args.prune < 1:
                print("error: --prune must be >= 1", file=sys.stderr)
                return 2
            removed = prune_runs(conn, args.prune)
            print(f"Pruned {removed} log row(s); kept last {args.prune} per source.")
            return 0

        latest = latest_runs(conn)
        successes = last_successes(conn)

        if args.status:
            print_status(sources, latest, successes, now)
            return 0
        if args.errors:
            print_errors(latest, now)
            return 0

        jobs, skipped = select_jobs(
            sources,
            force=args.force,
            successes=successes,
            now=now,
            limit=args.limit,
        )

        if args.dry_run:
            print(f"{len(jobs)} source(s) due, {len(skipped)} not due.")
            for job in jobs:
                last_ok = successes.get(job.source.name)
                print(
                    f"  would crawl [{job.source.frequency}] {job.source.name} "
                    f"(last ok: {human_delta(last_ok, now)})"
                )
            return 0

        if not jobs:
            print("Nothing due. Use --force to crawl everything, --status to review.")
            return 0

        settings = {
            "max_depth": args.max_depth,
            "max_pages": args.max_pages,
            "max_assets": 0 if args.no_assets else args.max_assets,
            "magic": not args.no_magic,
            "stealth": not args.no_stealth,
            "page_timeout_s": args.timeout,
            "retries": args.retries,
            "concurrency": args.concurrency,
            "save_html": args.save_html,
            "api": args.api,
            "api_render": not args.no_api_render,
            "api_concurrency": args.api_concurrency,
        }
        backend = Crawl4AIBackend(
            timeout_s=args.timeout,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
            max_assets=args.max_assets,
            fetch_assets=not args.no_assets,
            magic=not args.no_magic,
            stealth=not args.no_stealth,
            retries=args.retries,
            api_provider=args.api,
            api_key=args.api_key,
            api_template=args.api_template,
            api_render=not args.no_api_render,
            api_concurrency=args.api_concurrency,
        )
        print(
            f"Crawling {len(jobs)} source(s) with {backend.name} "
            f"(depth {args.max_depth}, max {args.max_pages} pages/seed, "
            f"max {args.max_assets} assets/source, concurrency {args.concurrency})..."
        )
        records = asyncio.run(
            _run(backend, jobs, conn, args.output_dir, args.concurrency, settings, args.save_html)
        )

        ok = sum(1 for record in records if record.success)
        failed = len(records) - ok
        print(f"\nDone: {ok} succeeded, {failed} failed, {len(skipped)} skipped (not due).")
        if failed:
            print("Run with --errors to see failures, --status for the full log.")
        if args.fail_on_error and failed:
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        conn.close()


async def _run(
    backend: Crawl4AIBackend,
    jobs: list[Job],
    conn: sqlite3.Connection,
    output_dir: Path,
    concurrency: int,
    settings: dict[str, Any],
    save_html: bool,
) -> list[RunRecord]:
    async with backend:
        return await run_jobs(
            jobs, backend, conn, output_dir, concurrency, settings, save_html
        )


if __name__ == "__main__":
    sys.exit(main())