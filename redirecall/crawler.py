# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.crawler — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import httpx
import redis
try:
    import trafilatura   # Best-in-class web content extraction
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
try:
    from bs4 import BeautifulSoup   # HTML parsing fallback + link extraction
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
try:
    from crawl4ai import AsyncWebCrawler as _C4AIWebCrawler   # parallel JS-capable crawler
    from crawl4ai import BrowserConfig  as _C4AIBrowserConfig
    from crawl4ai import CrawlerRunConfig as _C4AIRunConfig
    HAS_CRAWL4AI = True
except ImportError:
    HAS_CRAWL4AI = False
import platform as _platform
_BROWSER_EXTRA_ARGS: list[str] = [
    "--disable-dev-shm-usage",           # avoid /dev/shm exhaustion in containers
    "--disable-gpu",                     # no GPU needed for headless text extraction
    "--disable-background-networking",   # suppress background pings (Safe Browsing etc.)
    "--disable-sync",                    # no Chrome account sync
    "--no-first-run",                    # skip first-run wizard
    "--disable-extensions",              # no extensions
    "--blink-settings=imagesEnabled=false",  # block image rendering at the engine level
]
if _platform.system() == "Linux":
    _BROWSER_EXTRA_ARGS += ["--no-sandbox", "--disable-setuid-sandbox"]
from . import config, ingest, rag, rag_admin, state

log = logging.getLogger(__name__)

# How long the crawl teardown waits for the embed worker to flush its last batch. Bounded
# so a wedged worker cannot hang the `finally` — and with it the whole crawl task — forever.
_EMBED_DRAIN_TIMEOUT = 30.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEB CRAWLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Shared httpx client — connection pooling + keep-alive reused across all crawls.
# Created lazily on first use; never closed (lives for the server process lifetime).


async def _get_http_client() -> httpx.AsyncClient:
    """Return (or lazily create) the shared pooled httpx AsyncClient.

    Redirects are followed manually in fetch_url (not by httpx) so the SSRF
    guard can re-validate every hop — a page on a public host could otherwise
    302 the crawler onto an internal address.
    """
    if state._shared_http_client is None or state._shared_http_client.is_closed:
        state._shared_http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            headers={"User-Agent": "RediRecallBot/1.0"},
        )
    return state._shared_http_client


def _ip_is_public(ip_str: str) -> bool:
    """True only for globally-routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """SSRF guard: raise ValueError unless ``url`` is a public http(s) target.

    Rejects non-http(s) schemes and resolves the host, requiring *every* A/AAAA
    record to be a public address — so a hostname that resolves to a private,
    loopback, link-local, or reserved IP (e.g. 127.0.0.1, 169.254.169.254,
    10.x, cloud metadata endpoints) is blocked before any connection is made.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"blocked non-http(s) URL scheme: {p.scheme or '(none)'}")
    host = p.hostname
    if not host:
        raise ValueError("blocked URL with no host")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host '{host}': {e}")
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise ValueError(f"host '{host}' did not resolve to any address")
    for addr in addrs:
        if not _ip_is_public(addr):
            raise ValueError(f"blocked private/reserved address {addr} for host '{host}'")


async def fetch_url(url: str) -> str:
    """Fetch a URL via the shared pooled client, guarding against SSRF.

    Every request — the initial URL and each redirect hop — is validated by
    assert_public_url() before the connection is made. 4xx responses return an
    empty string (page missing/forbidden — skip silently); 5xx responses raise
    so they surface as errors in the crawl log.
    """
    client = await _get_http_client()
    current = _strip_fragment(url)
    for _ in range(10):  # bounded redirect chain
        assert_public_url(current)          # re-checked on every hop
        r = await client.get(current)
        if r.is_redirect:
            location = r.headers.get("location")
            if not location:
                break
            current = _strip_fragment(urljoin(current, location))
            continue
        if 400 <= r.status_code < 500:
            return ""
        r.raise_for_status()
        return r.text
    raise ValueError(f"too many redirects fetching {url}")


def _assert_c4ai_result_public(r, requested_url: str) -> None:
    """Re-check a crawl4ai result's final (post-redirect) URL against the SSRF guard.

    The headless browser follows redirects itself with no per-hop validation, so
    a public seed that redirects to an internal host would otherwise be fetched
    and its content indexed. If the landing URL is internal this raises, and the
    caller drops the page (nothing is indexed or shown to the user).
    """
    final = getattr(r, "redirected_url", None) or getattr(r, "url", None) or ""
    if final and final != requested_url:
        assert_public_url(final)


def is_llms_txt(url: str, content: str) -> bool:
    """Return True if the URL points to an llms.txt / llms-full.txt manifest."""
    return url.endswith("llms.txt") or url.endswith("llms-full.txt")


def parse_llms_txt(content: str, base_url: str) -> list[dict]:
    """
    Parse an llms.txt manifest and return a list of {url, description} dicts.

    The llms.txt spec (https://llmstxt.org/) uses Markdown-style links:
        - [Page Title](https://example.com/page): optional description

    We also handle bare absolute URLs (one per line) as a fallback.
    Comment lines (#) and blockquotes (>) are ignored.
    Duplicate URLs are deduplicated.
    """
    links = []
    seen: set[str] = set()

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue

        # Extract all [text](url) patterns on this line
        for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", line):
            desc, href = m.group(1), m.group(2)
            if href.startswith("http") or href.startswith("/"):
                full = _strip_fragment(urljoin(base_url, href))
                if full not in seen:
                    seen.add(full)
                    links.append({"url": full, "description": desc})

        # Bare absolute URL on its own line (alternative llms.txt format)
        bare = re.match(r"^(https?://\S+)$", line)
        if bare:
            full = _strip_fragment(bare.group(1))
            if full not in seen:
                seen.add(full)
                links.append({"url": full, "description": full})

    return links


def extract_text(html: str, url: str) -> str:
    """
    Extract clean readable text from a fetched page.

    Plain-text and Markdown files (URL ends in .md/.txt, or content has no
    HTML tags) are returned directly — no stripping needed and HTML parsers
    would mangle them.  For real HTML pages we try trafilatura first (best
    quality), then BeautifulSoup, then fall back to the raw content.
    """
    url_lower = url.split("?")[0].lower()
    if url_lower.endswith(".md") or url_lower.endswith(".txt"):
        return html.strip()

    stripped = html.strip()
    if stripped and not stripped.startswith("<"):
        return stripped

    if HAS_TRAFILATURA:
        t = trafilatura.extract(html, include_links=False, include_images=False)
        if t:
            return t
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    return html


def _strip_fragment(url: str) -> str:
    """Remove the #anchor from a URL — same page regardless of anchor."""
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


# File extensions that are never useful to crawl as text content.
# Binary downloads, media, archives, executables, and data files.
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    # Archives / compressed
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".rar", ".7z",
    # Executables / installers
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".appimage",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    # Audio / video
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv", ".webm",
    # Documents (handled separately via file upload, not crawl)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    # Data / binary
    ".bin", ".dat", ".iso", ".img", ".whl", ".jar",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot",
})


def _is_crawlable_url(url: str) -> bool:
    """Return False if the URL points to a known binary/non-text file type."""
    path = urlparse(url).path.lower().split("?")[0]
    _, ext = os.path.splitext(path)
    return ext not in _SKIP_EXTENSIONS


def _extract_html_links(html: str, base_url: str) -> list[str]:
    """Return all absolute HTTP(S) links found in an HTML page, excluding binary file types."""
    if not HAS_BS4:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = _strip_fragment(urljoin(base_url, a["href"]))
            p = urlparse(href)
            if p.scheme in ("http", "https") and p.netloc and _is_crawlable_url(href):
                links.append(href)
        return links
    except Exception:
        return []


_robots_cache: dict[str, RobotFileParser] = {}

def can_crawl(url: str) -> bool:
    """
    Check robots.txt to see if RediRecallBot is permitted to crawl this URL.
    Caches the parsed robots.txt per netloc so the same domain is only
    fetched once per crawl session (not once per page).
    Returns True on any error (conservative: allow if unsure).
    """
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc not in _robots_cache:
            robots_url = f"{parsed.scheme}://{netloc}/robots.txt"
            # SSRF guard: robots.txt is fetched before the page, so validate the
            # host here too — otherwise this is a blind GET against an internal
            # address for any URL that reached the crawler unvalidated.
            assert_public_url(robots_url)
            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            _robots_cache[netloc] = rp
        return _robots_cache[netloc].can_fetch("RediRecallBot", url)
    except Exception:
        return True


async def crawl_url(
    instance: str,
    url: str,
    depth: int = 0,
    visited: set | None = None,       # unused — kept for caller compatibility
    progress_cb=None,
    respect_robots: bool = True,
    local_only: bool = True,
    path_prefix_only: bool = False,
    max_pages: int = 0,
    _root_netloc: str = "",            # unused — kept for caller compatibility
    _counter: dict | None = None,      # unused — kept for caller compatibility
    rc: redis.Redis | None = None,
    concurrency: int = 10,
    js_render: bool = False,
    js_concurrency: int = 3,
    smart_mode: bool = True,
    min_words: int = 100,
    force_reindex: bool = False,
    stats: dict | None = None,
):
    """
    Parallel BFS web crawler with optional JavaScript rendering.

    ``stats``, when given, is a caller-owned dict this crawl keeps up to date in place
    with ``discovered`` (URLs the frontier has ever admitted) and ``queued`` (URLs still
    waiting). A BFS over an unknown site has no total to count towards, so pages-done
    against pages-discovered is the only honest progress ratio available — the UI shows
    an indeterminate bar without it whenever max_pages is 0, which is the default.

    Architecture
    ------------
    • BFS queue (asyncio.Queue) drives link discovery — breadth-first so
      shallow pages are indexed first and progress is visible immediately.
    • asyncio.Semaphore(concurrency) limits how many pages are in-flight at
      once without spawning an unbounded number of tasks.
    • A single shared httpx.AsyncClient is reused across all requests in the
      crawl for connection keep-alive and HTTP/2 multiplexing (fast path).
    • With js_render=True (requires crawl4ai + playwright) a single browser
      context is opened once and reused across all workers — far cheaper than
      launching a new browser tab per page.

    Speed improvement vs old sequential recursive crawler
    -----------------------------------------------------
    Old: each page was fetch→extract→embed→store before the next URL was even
         requested.  100 pages × 250 ms avg latency = ~25 s minimum wall time.
    New: up to `concurrency` (default 10) pages fetched concurrently.
         100 pages ÷ 10 workers × 250 ms = ~2.5 s minimum (≈10× faster).
    """
    seed_url    = _strip_fragment(url)
    _seed_parsed = urlparse(seed_url)
    root_netloc  = _seed_parsed.netloc
    # Derive path prefix: use the directory portion of the seed URL's path.
    # e.g. http://my.org/list/3/       → prefix "/list/3/"
    #      http://my.org/list/3/index  → prefix "/list/3/"
    _seed_path = _seed_parsed.path
    if not _seed_path.endswith("/"):
        _seed_path = _seed_path.rsplit("/", 1)[0] + "/"
    root_path_prefix = _seed_path   # used when path_prefix_only=True

    # Clear per-crawl robots.txt cache so stale results don't carry over
    _robots_cache.clear()

    # ── Shared mutable state (all workers in the same crawl share these) ───
    visited_urls: set[str]   = set()
    visited_lock             = asyncio.Lock()
    counter                  = {"count": 0}
    sem                      = asyncio.Semaphore(max(1, concurrency))
    queue: asyncio.Queue     = asyncio.Queue()

    def _note_frontier() -> None:
        """Publish the live frontier size into the caller's stats dict.

        len(visited_urls) is the discovered count rather than a separate counter: a URL
        enters visited_urls exactly once, at the moment it is admitted to the frontier,
        so the set already IS the tally and cannot drift from it.

        `discovered` is deliberately the count of URLs ADMITTED, not of pages indexed:
        a URL is admitted before the crawlable-type, already-indexed, robots, empty-text
        and duplicate-content checks, any of which can end it without an index. Progress
        must therefore be measured as pages RESOLVED (indexed + skipped + blocked +
        errored) over discovered — resolved reaches discovered when the crawl drains.
        Dividing pages_done alone by this number stalls: on an incremental re-crawl where
        most pages are already indexed, pages_done stays near zero while discovered climbs.
        `resolved` is published here so a caller does not have to sum four counters and
        get that wrong.
        """
        if stats is not None:
            stats["discovered"] = len(visited_urls)
            stats["queued"] = queue.qsize()
            stats["resolved"] = _resolved["n"]

    # Pages that have reached a terminal state. Counted from the crawler's own progress
    # callback rather than summed from counters the caller happens to keep, so it is
    # correct for every caller and can be asserted without one.
    _resolved = {"n": 0}
    _TERMINAL_STATUS = ("indexed", "skipped", "blocked", "error")
    _user_progress_cb = progress_cb

    async def progress_cb(u, status, n=0, err="", count=0):   # noqa: F811 — wraps the arg
        if status in _TERMINAL_STATUS:
            _resolved["n"] += 1
            if stats is not None:
                stats["resolved"] = _resolved["n"]
        if _user_progress_cb is not None:
            await _user_progress_cb(u, status, n, err, count)

    visited_urls.add(seed_url)
    queue.put_nowait((seed_url, depth))
    _note_frontier()

    # ── URL-level deduplication across crawl sessions ──────────────────────
    # Load the full indexed-URL set into memory once at crawl start.
    # All per-page checks are then O(1) Python set lookups — zero Redis
    # round-trips and zero thread-pool overhead during the crawl.
    _crawl_rc         = rc or rag_admin.rc_for_instance(instance)
    _indexed_urls_key = f"rag:{instance}:indexed_urls"
    if force_reindex:
        # Wipe the URL skip-list so every page is fetched and re-embedded fresh
        try:
            await asyncio.to_thread(_crawl_rc.delete, _indexed_urls_key)
        except Exception:
            pass
        _indexed_urls_mem: set[str] = set()
    else:
        try:
            # Loading the whole indexed-URL set is one sync command; keep it off the loop
            # so a large skip-list doesn't stall chat at crawl start.
            members = await asyncio.to_thread(_crawl_rc.smembers, _indexed_urls_key)
            _indexed_urls_mem = {u.decode() if isinstance(u, bytes) else u for u in members}
        except Exception:
            _indexed_urls_mem = set()

    # Collect newly indexed URLs in memory; flush to Redis in a single pipeline
    # after the crawl finishes rather than one SADD per page.
    _newly_indexed: list[str] = []
    _newly_indexed_lock = asyncio.Lock()

    async def _mark_url_indexed(u: str) -> None:
        _indexed_urls_mem.add(u)
        async with _newly_indexed_lock:
            _newly_indexed.append(u)
            # Flush to Redis every 50 URLs so indexed_urls survives cancellation
            if len(_newly_indexed) % 50 == 0:
                to_flush = list(_newly_indexed)
                try:
                    pipe = _crawl_rc.pipeline(transaction=False)
                    for fu in to_flush:
                        pipe.sadd(_indexed_urls_key, fu)
                    await asyncio.to_thread(pipe.execute)   # only the round-trip blocks
                except Exception:
                    pass

    # ── Inner: fetch, extract, ingest one page and enqueue its children ────
    async def process_page(page_url: str, page_depth: int, fetch_fn):
        # ── Cheap pre-checks BEFORE acquiring the semaphore ────────────────
        # These are all O(1) in-memory lookups — no I/O, no threads needed.
        if max_pages > 0 and counter["count"] >= max_pages:
            return
        if not _is_crawlable_url(page_url):
            if progress_cb:
                await progress_cb(page_url, "skipped", 0, "binary file type", counter["count"])
            return
        if not is_llms_txt(page_url, "") and page_url in _indexed_urls_mem:
            if progress_cb:
                await progress_cb(page_url, "skipped", 0, "already indexed", counter["count"])
            return
        # robots.txt: if domain already cached, check inline; otherwise fetch in thread
        if respect_robots:
            parsed_url = urlparse(page_url)
            if parsed_url.netloc in _robots_cache:
                # Cache hit — synchronous dict lookup, no thread needed
                if not _robots_cache[parsed_url.netloc].can_fetch("RediRecallBot", page_url):
                    if progress_cb:
                        await progress_cb(page_url, "blocked", 0, "", counter["count"])
                    return
            else:
                # Cache miss — fetch robots.txt in a thread (blocks until done)
                if not await asyncio.to_thread(can_crawl, page_url):
                    if progress_cb:
                        await progress_cb(page_url, "blocked", 0, "", counter["count"])
                    return

        async with sem:
            try:
                # Re-check max_pages inside the semaphore (counter may have advanced)
                if max_pages > 0 and counter["count"] >= max_pages:
                    return

                if progress_cb:
                    await progress_cb(page_url, "crawling", 0, "", counter["count"])

                raw_content, discovered_links = await fetch_fn(page_url)

                # llms.txt manifest → parse links and queue them all
                if is_llms_txt(page_url, raw_content):
                    manifest_links = parse_llms_txt(raw_content, page_url)
                    if progress_cb:
                        await progress_cb(page_url, "parsed_llms_txt",
                                          len(manifest_links), "", counter["count"])
                    for lnk in manifest_links:
                        lurl = _strip_fragment(lnk["url"])
                        if max_pages > 0 and counter["count"] >= max_pages:
                            break
                        async with visited_lock:
                            if lurl in visited_urls:
                                continue
                            visited_urls.add(lurl)
                        queue.put_nowait((lurl, 0))  # depth=0: don't follow further links
                    return

                # Regular page: extract text → chunk+dedup → hand off to embed worker
                # js_render path: raw_content is already clean markdown from crawl4ai
                text = raw_content if js_render else extract_text(raw_content, page_url)
                if not text.strip():
                    if progress_cb:
                        await progress_cb(page_url, "skipped", 0, "empty content", counter["count"])
                    return

                # _prepare_chunks: CPU-light (no model calls) — chunk, dedup, reserve IDs
                records = await asyncio.to_thread(ingest._prepare_chunks, instance, text, page_url, _crawl_rc, force_reindex)
                # Always mark URL as visited so future crawls don't re-fetch duplicate-content pages
                await _mark_url_indexed(page_url)
                if not records:
                    if progress_cb:
                        await progress_cb(page_url, "skipped", 0, "duplicate content", counter["count"])
                    return

                counter["count"] += 1
                # Embed worker picks this up and calls progress_cb("indexed", ...) after flush
                await embed_queue.put((page_url, records))

                # Enqueue child links if depth allows
                if page_depth > 0:
                    for href in discovered_links:
                        if max_pages > 0 and counter["count"] >= max_pages:
                            break
                        href = _strip_fragment(href)
                        parsed_href = urlparse(href)
                        if local_only and parsed_href.netloc != root_netloc:
                            continue
                        if path_prefix_only and not parsed_href.path.startswith(root_path_prefix):
                            continue
                        async with visited_lock:
                            if href in visited_urls:
                                continue
                            visited_urls.add(href)
                        queue.put_nowait((href, page_depth - 1))

            except Exception as e:
                config.append_log({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "instance": instance, "source": page_url,
                    "chunks": 0, "status": "error", "error": str(e),
                })
                if progress_cb:
                    await progress_cb(page_url, "error", 0, str(e), counter["count"])

    # ── Cross-page embed queue — decouples fetch workers from model calls ───
    # process_page puts (page_url, records) here after chunking/dedup.
    # embed_worker drains it in batches so encode() is called once per batch
    # instead of once per page — amortises the ~100 ms per-call model overhead.
    embed_queue: asyncio.Queue = asyncio.Queue()

    # ── BFS driver: N concurrent workers pulling from the shared queue ─────
    async def run_bfs(fetch_fn):
        EMBED_BATCH = 64      # max chunks per encode() call
        EMBED_FLUSH_MS = 0.10 # seconds to wait before flushing a partial batch

        async def embed_worker():
            """Drain embed_queue in cross-page batches; call add_chunks once per batch."""
            batch_records: list[dict] = []
            batch_meta: list[tuple[str, int]] = []   # (page_url, n_chunks)

            async def flush():
                if not batch_records:
                    return
                records_snap = list(batch_records)
                meta_snap    = list(batch_meta)
                batch_records.clear()
                batch_meta.clear()
                try:
                    await asyncio.to_thread(rag.add_chunks, instance, records_snap, _crawl_rc)
                except Exception as e:
                    log.warning("embed_worker: batch embed/store failed (%s) — %d chunks lost", e, len(records_snap))
                    for page_url, n in meta_snap:
                        config.append_log({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "instance": instance, "source": page_url,
                            "chunks": 0, "status": "error", "error": str(e),
                        })
                        if progress_cb:
                            await progress_cb(page_url, "error", 0, str(e), counter["count"])
                    return
                for page_url, n in meta_snap:
                    config.append_log({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "instance": instance, "source": page_url,
                        "chunks": n, "status": "ok",
                    })
                    if progress_cb:
                        await progress_cb(page_url, "indexed", n, "", counter["count"])

            while True:
                try:
                    item = await asyncio.wait_for(embed_queue.get(), timeout=EMBED_FLUSH_MS)
                except asyncio.TimeoutError:
                    await flush()
                    continue
                except asyncio.CancelledError:
                    await flush()
                    raise

                if item is None:   # sentinel — crawl finished
                    await flush()
                    break

                page_url, records = item
                batch_records.extend(records)
                batch_meta.append((page_url, len(records)))
                if len(batch_records) >= EMBED_BATCH:
                    await flush()

        gate = state._crawl_gates.get(seed_url)

        async def worker():
            while True:
                page_url, page_depth = await queue.get()
                try:
                    if gate is not None and not gate.is_set():
                        # Paused: hold here without consuming the item's slot in
                        # the queue's unfinished count, so join() still waits.
                        await gate.wait()
                    await process_page(page_url, page_depth, fetch_fn)
                except Exception as e:
                    log.warning("worker: unhandled exception for %s: %s", page_url, e)
                finally:
                    queue.task_done()
                    _note_frontier()   # a page just finished discovering its children

        num_workers = min(max(1, concurrency), 50)
        embed_task   = asyncio.create_task(embed_worker())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(num_workers)]

        try:
            await queue.join()   # blocks until every queued item has been task_done()
        finally:
            # Also on the cancel path. _note_frontier otherwise runs only in the worker's
            # finally, and a worker cancelled while parked on queue.get() never reaches it
            # — so a cancelled crawl was left advertising a queue it no longer has.
            _note_frontier()
            for t in worker_tasks:
                t.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)

            # Shutdown belongs in the finally, not after it. Cancelling a crawl makes
            # queue.join() raise CancelledError, which skipped everything below the
            # try/finally — so embed_worker's `while True` kept spinning at 10 Hz for
            # the life of the process, one orphan per cancelled crawl, each pinning its
            # closure, and the pages already indexed were never recorded.
            cancelled_during_drain = False
            try:
                embed_queue.put_nowait(None)   # unbounded queue: never blocks
                # Bounded: if the worker wedges, this finally must not wedge with it.
                await asyncio.wait_for(asyncio.shield(embed_task), timeout=_EMBED_DRAIN_TIMEOUT)
            except asyncio.CancelledError:
                # Being torn down while draining. Stop the worker (it catches
                # CancelledError and flushes first), then RE-RAISE below — swallowing it
                # here made the crawl task report success and left task.cancelled() False,
                # so callers could not tell a cancelled crawl from a finished one.
                cancelled_during_drain = True
                embed_task.cancel()
                await asyncio.gather(embed_task, return_exceptions=True)
            except asyncio.TimeoutError:
                log.warning("crawl: embed worker did not drain in "
                            f"{_EMBED_DRAIN_TIMEOUT}s — cancelling it; the last batch may be lost")
                embed_task.cancel()
                await asyncio.gather(embed_task, return_exceptions=True)
            except Exception as e:
                # A worker failure outside flush()'s own try (progress_cb, append_log, a
                # loop bug) used to be discarded with no log at all, and the crawl still
                # reported success.
                log.warning(f"crawl: embed worker failed during shutdown: {e!r}")
                embed_task.cancel()
                await asyncio.gather(embed_task, return_exceptions=True)

            # Flush all newly indexed URLs to Redis in a single pipeline
            if _newly_indexed:
                try:
                    pipe = _crawl_rc.pipeline(transaction=False)
                    for u in _newly_indexed:
                        pipe.sadd(_indexed_urls_key, u)
                    await asyncio.to_thread(pipe.execute)   # only the round-trip blocks
                except Exception as e:
                    log.warning(f"crawl: could not record {len(_newly_indexed)} indexed "
                                f"URLs — they will be re-crawled next time: {e!r}")

            # Cancellation must still propagate: a caller that awaits this task has to be
            # able to distinguish "cancelled" from "completed".
            if cancelled_during_drain:
                raise asyncio.CancelledError

    # ── Helpers shared by all browser-based fetch paths ────────────────────
    def _links_from_result(r) -> list[str]:
        """Extract absolute HTTP links from a crawl4ai result object."""
        return (
            [l["href"] for l in r.links.get("internal", []) if l.get("href", "").startswith("http")] +
            [l["href"] for l in r.links.get("external", []) if l.get("href", "").startswith("http")]
        )

    async def fetch_httpx(u: str) -> tuple[str, list[str]]:
        html  = await fetch_url(u)
        links = _extract_html_links(html, u)
        return html, links

    # ── Resolve which modes are actually available ──────────────────────────
    use_js    = js_render
    use_smart = smart_mode and not js_render   # smart = httpx-first + JS fallback

    if (use_js or use_smart) and not HAS_CRAWL4AI:
        if use_js:
            log.warning(
                "js_render=True but crawl4ai not installed — "
                "falling back to httpx. Run: pip install crawl4ai && playwright install chromium"
            )
        use_js    = False
        use_smart = False

    # ── Browser config — shared by both full-JS and smart-fallback paths ───
    # One browser process is reused across ALL worker coroutines for the
    # lifetime of this crawl.  Extra args block image rendering and suppress
    # background network activity, cutting per-tab RAM/CPU by ~60%.
    _browser_cfg = _C4AIBrowserConfig(
        headless=True,
        verbose=False,
        extra_args=_BROWSER_EXTRA_ARGS,
    ) if HAS_CRAWL4AI else None

    # domcontentloaded fires as soon as HTML is parsed — no need to wait for
    # analytics/tracking calls that never resolve on networkidle.
    _run_cfg = _C4AIRunConfig(
        wait_until="domcontentloaded",   # was "networkidle" — 3-10× faster per page
        page_timeout=15_000,             # was 30 000 ms
        word_count_threshold=10,
        exclude_all_images=True,         # skip image downloads entirely
        exclude_external_images=True,
    ) if HAS_CRAWL4AI else None

    # Separate semaphore caps Playwright tabs independently of httpx workers.
    # Browser tabs are ~150 MB each; default cap of 3 keeps RAM predictable.
    _js_sem = asyncio.Semaphore(max(1, js_concurrency))

    # ── Dispatch ────────────────────────────────────────────────────────────
    if use_js:
        # Full JS mode: every page goes through Playwright.
        async with _C4AIWebCrawler(config=_browser_cfg) as _c4ai:
            async def fetch_js(u: str) -> tuple[str, list[str]]:
                assert_public_url(u)          # block internal targets before the browser fetch
                async with _js_sem:
                    r = await _c4ai.arun(u, config=_run_cfg)
                _assert_c4ai_result_public(r, u)   # block redirect-to-internal
                text = (r.markdown.fit_markdown
                        if (r.markdown and r.markdown.fit_markdown)
                        else (r.html or ""))
                return text, _links_from_result(r)

            await run_bfs(fetch_js)

    elif use_smart:
        # Smart mode: try httpx first (fast, zero browser overhead).
        # Only pages whose extracted text is below min_words are retried
        # with Playwright — typically 5-15% of pages in a real docs crawl.
        async with _C4AIWebCrawler(config=_browser_cfg) as _c4ai:
            async def fetch_smart(u: str) -> tuple[str, list[str]]:
                # SSRF guard for BOTH the httpx and browser paths. Raised here,
                # OUTSIDE the try below, so an internal-target rejection is never
                # swallowed into the browser fallback (which would defeat it).
                assert_public_url(u)
                # ── Fast path: pooled httpx ────────────────────────────────
                httpx_html:  str       = ""
                httpx_links: list[str] = []
                try:
                    httpx_html, httpx_links = await fetch_httpx(u)
                    text = extract_text(httpx_html, u)
                    if len(text.split()) >= min_words:
                        return httpx_html, httpx_links   # sufficient content, done
                    if not httpx_html:
                        # Empty response (4xx) — JS won't help; skip browser entirely
                        return httpx_html, httpx_links
                    log.debug("smart-crawl: thin content (%d words) on %s — retrying with JS",
                              len(text.split()), u)
                except Exception as exc:
                    log.debug("smart-crawl: httpx failed for %s (%s) — retrying with JS", u, exc)

                # ── JS fallback: Playwright (rate-limited by _js_sem) ──────
                try:
                    async with _js_sem:
                        r = await _c4ai.arun(u, config=_run_cfg)
                    _assert_c4ai_result_public(r, u)   # block redirect-to-internal
                    text = (r.markdown.fit_markdown
                            if (r.markdown and r.markdown.fit_markdown)
                            else (r.html or ""))
                    return text, _links_from_result(r)
                except Exception as js_exc:
                    # Browser context was None or crashed (e.g. after a cancelled crawl).
                    # Return whatever httpx gave us rather than failing the page entirely.
                    log.debug("smart-crawl: JS fallback failed for %s (%s) — using httpx result", u, js_exc)
                    return httpx_html, httpx_links

            await run_bfs(fetch_smart)

    else:
        # Pure httpx — no browser, maximum speed for pre-rendered HTML sites.
        await run_bfs(fetch_httpx)

