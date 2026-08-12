# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.ws — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import time
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import config, crawler, rag_admin, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEBSOCKET MANAGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConnManager:
    """Tracks active WebSocket connections by session ID."""

    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, ws: WebSocket, sid: str):
        await ws.accept()
        self.active[sid] = ws

    def disconnect(self, sid: str):
        self.active.pop(sid, None)

    async def send(self, sid: str, data: dict):
        ws = self.active.get(sid)
        if ws:
            await ws.send_json(data)


mgr = ConnManager()

async def _recrawl_loop():
    """
    Background task: periodically re-crawl all scheduled web sources.
    Runs every 60 seconds internally; actual crawls only trigger when
    ``now - last_crawled >= interval_minutes * 60``.
    """
    while True:
        await asyncio.sleep(60)
        if not state._config.get("recrawl", {}).get("enabled", False):
            continue
        interval_secs = int(state._config.get("recrawl", {}).get("interval_minutes", 60)) * 60
        now = time.time()
        scheduled = state._config.get("scheduled_sources", [])
        if not scheduled:
            continue
        changed = False
        for src in scheduled:
            last = float(src.get("last_crawled", 0))
            if now - last < interval_secs:
                continue
            url      = src.get("url", "").strip()
            instance = src.get("instance", "default")
            depth    = int(src.get("depth", 0))
            if not url:
                continue
            log.info(f"Recrawl scheduler: crawling {url} → instance '{instance}'")
            try:
                rc = rag_admin.rc_for_instance(instance)
                await crawler.crawl_url(instance, url, depth, rc=rc)
                src["last_crawled"] = now
                changed = True
            except Exception as e:
                log.warning(f"Recrawl failed for {url}: {e}")
        if changed:
            config.save_config(state._config)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WATCHED FOLDERS — local-disk twin of the recrawl scheduler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# What each watched file looked like when last ingested. Fields are keyed
# "{instance}\x00{abs_path}" (the same folder may feed several instances, and a
# retargeted folder must re-ingest into the new one); values are "mtime_ns:size".
_WATCH_SEEN_KEY = "watch:seen"
_watch_last_scan = 0.0


def _watch_candidates(root, accept: set):
    """(path, signature) for every supported file under a watch root. Dot-entries
    (a .git/.venv directory, .hidden files) are filtered out after enumeration —
    rglob still walks them, this only keeps their contents out of the index.
    Runs in a worker thread: rglob + stat are blocking disk I/O."""
    out = []
    for p in root.rglob("*"):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file() and p.suffix.lower() in accept:
            try:
                st = p.stat()
                out.append((p, f"{st.st_mtime_ns}:{st.st_size}"))
            except OSError:
                continue   # vanished between listing and stat
    return out


def _watch_seen_sync(instance: str, entries: list, root) -> tuple[dict, list]:
    """One Redis round-trip per scan (worker thread): the previous signatures for
    this instance, plus pruning of fields whose file no longer exists under this
    root — the hash would otherwise grow forever as files come and go. (Only the
    signature bookkeeping is pruned; the chunks stay indexed by design.)"""
    from . import redis_store
    rc = redis_store.r()
    seen = {}
    stale = []
    current = {f"{instance}\x00{p}" for p, _ in entries}
    prefix = f"{instance}\x00{root}"
    for k, v in rc.hgetall(_WATCH_SEEN_KEY).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        seen[k] = v
        if k.startswith(prefix) and k not in current:
            stale.append(k)
    if stale:
        rc.hdel(_WATCH_SEEN_KEY, *stale)
    return seen, stale


async def _watch_folder_loop():
    """Background task: ingest new/changed files from the configured watched
    folders into their RAG instance. A changed file's previous chunks are removed
    first (the per-document delete releases its dedup hashes) so an edit replaces
    the old version instead of piling a second copy next to it. Files deleted
    from disk are deliberately left indexed — disappearing data on an unmounted
    share would be worse than a stale document; remove those via Documents.

    Every iteration is fully wrapped: this loop reads user-editable config, and a
    malformed value must skip a pass, not kill the scanner for the process's life."""
    global _watch_last_scan
    from pathlib import Path
    from . import ingest, redis_store, routes_sources   # late: routes load after ws
    while True:
        await asyncio.sleep(60)
        try:
            wf = state._config.get("watch_folders", {})
            if not wf.get("enabled", False):
                continue
            interval = max(1, int(wf.get("interval_minutes", 5) or 5)) * 60
            if time.time() - _watch_last_scan < interval:
                continue
            _watch_last_scan = time.time()
            for folder in wf.get("folders", []):
                if not isinstance(folder, dict):
                    continue
                root = Path(str(folder.get("path", ""))).expanduser()
                instance = folder.get("instance", "default") or "default"
                endpoint = folder.get("endpoint") or None
                if not root.is_dir():
                    log.warning(f"Watch folder missing (kept configured): {root}")
                    continue
                try:
                    entries = await asyncio.to_thread(
                        _watch_candidates, root, ingest._CHAT_FILE_ACCEPT)
                    seen, _ = await asyncio.to_thread(
                        _watch_seen_sync, instance, entries, root)
                    rc = rag_admin._rc_for(instance, endpoint)
                    for f, sig in entries:
                        try:
                            key = f"{instance}\x00{f}"
                            prev = seen.get(key)
                            if prev == sig:
                                continue
                            source = str(f.relative_to(root))
                            if prev is not None:   # changed → replace, don't duplicate
                                try:
                                    await asyncio.to_thread(
                                        routes_sources.api_delete_document,
                                        instance, source, endpoint)
                                except Exception:
                                    pass
                            result = await ingest.ingest_file(instance, f, source, rc)
                            if result.get("status") == "ok":
                                await asyncio.to_thread(
                                    lambda: redis_store.r().hset(_WATCH_SEEN_KEY, key, sig))
                                log.info(f"Watch: ingested {source} → '{instance}' "
                                         f"({result.get('chunks', 0)} chunks)")
                            else:
                                log.warning(f"Watch: {source}: "
                                            f"{result.get('error', result.get('status'))}")
                        except Exception as e:
                            log.warning(f"Watch: failed on {f}: {e}")
                except Exception as e:
                    log.warning(f"Watch scan failed for {root}: {e}")
        except Exception as e:
            log.warning(f"Watch pass skipped (bad config?): {e}")


