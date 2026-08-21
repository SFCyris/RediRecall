# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_ingestion — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import base64
import hashlib
import io
import json
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from . import appcore, config, constants, crawler, embeddings, ingest, rag, rag_admin, routes_instances
from . import state as _ns_state

log = logging.getLogger(__name__)


def _unlink_quietly(path: Path) -> None:
    """Remove an upload, logging rather than raising: a failure to tidy up must never
    abort the ingest stream that is still reporting real results to the user."""
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"Could not delete upload '{path}': {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — INGESTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/rag/{instance}/ingest/files")
async def api_ingest_files(instance: str, files: list[UploadFile] = File(...), endpoint: str | None = None):
    routes_instances._check_instance_name(instance)   # ingest can CREATE an instance
    rc = rag_admin._rc_for(instance, endpoint)
    results = []
    for f in files:
        dest, safe_name = constants.safe_upload_dest(f.filename)
        blob = await f.read()
        if len(blob) > config._MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"{f.filename!r} is {len(blob) // (1024 * 1024)} MB; the limit is "
                     f"{config._MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                     f"(raise REDIRECALL_MAX_UPLOAD_MB to change it)")
        dest.write_bytes(blob)
        result = await ingest.ingest_file(instance, dest, safe_name, rc)
        results.append(result)
        # Remove the uploaded file now that it has been indexed — the content
        # lives in Redis; keeping the file on disk serves no further purpose.
        try:
            dest.unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"Could not delete upload '{dest}': {e}")
    return results


@appcore.app.post("/api/rag/{instance}/ingest/files/stream")
async def api_ingest_files_stream(instance: str, files: list[UploadFile] = File(...), endpoint: str | None = None):
    """
    SSE-streaming file ingestion.  Processes files one-by-one and emits a
    progress event after each file so the UI can show a live progress bar.

    Events: {file, status, chunks, error, index, total}
    Final:  {done: true, total: N}
    """
    routes_instances._check_instance_name(instance)   # ingest can CREATE an instance
    rc = rag_admin._rc_for(instance, endpoint)

    # Buffer all uploads to disk first (so we can stream progress after)
    saved: list[tuple[Path, str]] = []
    for f in files:
        dest, safe_name = constants.safe_upload_dest(f.filename)   # strip path + verify containment
        blob = await f.read()
        # The same cap the non-streaming twin enforces. It was only ever checked there,
        # and this is the route the UI actually drives — so the limit did not apply on the
        # one path anybody takes. Files already written are removed before raising, or a
        # rejected batch leaves its predecessors in the uploads directory.
        if len(blob) > config._MAX_UPLOAD_BYTES:
            for done_path, _ in saved:
                _unlink_quietly(done_path)
            raise HTTPException(
                413, f"{f.filename!r} is {len(blob) // (1024 * 1024)} MB; the limit is "
                     f"{config._MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                     f"(raise REDIRECALL_MAX_UPLOAD_MB to change it)")
        dest.write_bytes(blob)
        saved.append((dest, safe_name))

    # Register the job before the first event so /api/ingest/active can see it and
    # /api/ingest/cancel can stop it. Without this a file ingest was unobservable and
    # unstoppable: losing the stream (a closed tab, a reload) left it indexing on with
    # nobody able to find it, let alone cancel it.
    job = uuid.uuid4().hex[:12]
    _ns_state.reap_finished_ingests()
    _ns_state._active_ingests[job] = {
        "job": job, "instance": instance, "endpoint": endpoint or "default",
        "total": len(saved), "index": 0, "ok": 0, "errors": 0,
        "files": [n for _, n in saved], "current": "",
        "start_ts": datetime.now(timezone.utc).isoformat(),
        # `started` flips when the generator actually runs. Everything that clears a job
        # lives in that generator's finally, so a response whose body is never iterated —
        # a client that disconnects before the first chunk — would otherwise leave the job
        # un-done for the life of the process, with its uploads still on disk. The UI
        # attaches to the first job that is not done, so one phantom freezes that panel
        # permanently. reap_finished_ingests expires an unstarted job instead.
        "started": False, "registered_at": time.time(),
        # Not returned to the browser (see api_ingest_active); the reaper needs it to
        # clean up after a job whose generator never ran.
        "upload_paths": [str(d) for d, _ in saved],
        "done": False, "cancelled": False,
    }
    cancel = asyncio.Event()
    _ns_state._ingest_cancels[job] = cancel

    async def generate():
        st = _ns_state._active_ingests[job]
        st["started"] = True
        remaining = list(saved)
        try:
            yield f"data: {json.dumps({'job': job, 'total': len(saved)})}\n\n"
            # First ingest on a fresh install blocks for as long as it takes to fetch the
            # sentence-transformer weights (~90 MB) with no output at all, which reads as a
            # hung upload. Say so before starting rather than after.
            if _ns_state._embed_model is None:
                yield ("data: " + json.dumps({
                    "stage": "model",
                    "message": "Loading the embedding model — the first run downloads it "
                               "(about 90 MB) before indexing starts",
                }) + "\n\n")
            for idx, (path, name) in enumerate(saved):
                if cancel.is_set():
                    break
                remaining = saved[idx:]
                st["index"], st["current"] = idx, name
                try:
                    result = await ingest.ingest_file(instance, path, name, rc)
                    # ingest_file RETURNS {"status": "error"|"skipped"} rather than raising
                    # (an unsupported type, a scanned PDF with no extractable text), so
                    # counting on the exception alone scored every failure as a success —
                    # the tally and /api/ingest/active both reported ok=N, errors=0 for a
                    # batch in which nothing was indexed.
                    status = result.get("status", "ok")
                    if status == "ok":
                        st["ok"] += 1
                    else:
                        st["errors"] += 1
                    yield f"data: {json.dumps({'file': name, 'status': status, 'chunks': result.get('chunks', 0), 'error': result.get('error', ''), 'index': idx, 'total': len(saved)})}\n\n"
                except Exception as e:
                    st["errors"] += 1
                    yield f"data: {json.dumps({'file': name, 'status': 'error', 'error': str(e), 'index': idx, 'total': len(saved)})}\n\n"
                finally:
                    # Always remove the uploaded file after indexing — success or
                    # failure — to prevent unbounded growth of the uploads directory.
                    _unlink_quietly(path)
                remaining = saved[idx + 1:]
            # Only a cancel that actually stopped something. One arriving while the last
            # file was being indexed leaves nothing unprocessed, and reporting that run as
            # "Cancelled — N indexed before stopping" misdescribes a complete ingest.
            st["cancelled"] = cancel.is_set() and bool(remaining)
            yield ("data: " + json.dumps({"done": True, "total": len(saved),
                                          "cancelled": st["cancelled"],
                                          "ok": st["ok"], "errors": st["errors"]}) + "\n\n")
        finally:
            # Reached on cancel and on client disconnect (GeneratorExit) alike. Files that
            # were never indexed have to be removed here or they sit in the uploads
            # directory for good — the per-file finally above only covers ones we started.
            for path, _ in remaining:
                _unlink_quietly(path)
            st["done"] = True
            _ns_state._ingest_cancels.pop(job, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # identity => GZipMiddleware forwards this SSE stream untouched (gzip buffering
        # would hold trickled progress events until the stream closes).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Content-Encoding": "identity"},
    )


@appcore.app.post("/api/rag/{instance}/optimize")
def api_optimize_rag(instance: str, endpoint: str | None = None):
    """
    Deduplicate chunks in a RAG instance.

    Two chunks are considered exact duplicates when their text is identical
    after case-folding and whitespace normalisation.  The first occurrence
    is kept; all subsequent duplicates are deleted.

    Returns: {removed, remaining, total_before}
    """
    rc = rag_admin._rc_for(instance, endpoint)
    prefix = rag.rag_prefix(instance)

    seen: dict[str, str] = {}   # hash → first key that owns it
    to_delete: list = []
    total_before = 0

    # Pipeline hget("text") in batches to avoid N round-trips
    _BATCH = 200
    batch_keys: list = []

    def _process_batch(bkeys: list) -> None:
        pipe = rc.pipeline(transaction=False)
        for bk in bkeys:
            pipe.hget(bk, "text")
        for bk, text_raw in zip(bkeys, pipe.execute()):
            if not text_raw:
                continue
            text = text_raw.decode() if isinstance(text_raw, bytes) else text_raw
            normalised = " ".join(text.lower().split())
            h = hashlib.sha256(normalised.encode()).hexdigest()
            key_s = bk.decode() if isinstance(bk, bytes) else bk
            if h in seen:
                to_delete.append(bk)
            else:
                seen[h] = key_s

    for k in rc.scan_iter(rag.rag_chunk_glob(instance), count=500):
        total_before += 1
        batch_keys.append(k)
        if len(batch_keys) >= _BATCH:
            _process_batch(batch_keys)
            batch_keys = []
    if batch_keys:
        _process_batch(batch_keys)

    if to_delete:
        # Delete in batches to avoid oversized commands
        for i in range(0, len(to_delete), 500):
            rc.delete(*to_delete[i:i + 500])

    return {
        "removed":      len(to_delete),
        "remaining":    total_before - len(to_delete),
        "total_before": total_before,
    }


@appcore.app.get("/api/crawl/active")
async def api_crawl_active():
    """Return the state of all currently running (and recently finished) crawls."""
    return list(_ns_state._active_crawls.values())


# Server-side bookkeeping the browser has no use for. upload_paths in particular is an
# absolute filesystem path, which nothing outside this process should be handed.
_INGEST_PRIVATE_FIELDS = ("upload_paths", "registered_at")


# A source label is what the Documents view groups on and what the per-document delete
# addresses, so it has to stay short enough to read in a table and to round-trip through
# a TAG query.
_MAX_SOURCE_LEN = 400


@appcore.app.post("/api/rag/{instance}/ingest/text")
async def api_ingest_text(instance: str, payload: dict, endpoint: str | None = None):
    """Index a block of text supplied directly, with no file or URL behind it.

    The corpus could only ever be fed from a file or a crawl, so an answer worth keeping
    had nowhere to go: the semantic cache expires (``cache.ttl``, one hour by default),
    the conversation expires (``sessions.ttl``, one day), and the pin is browser-session
    state that does not survive a reload. This is the missing route.

    Body: ``{text, source}``. ``source`` is the caller's label for the text — it is what
    the Documents view groups on and what ``DELETE /api/rag/{instance}/documents``
    addresses, so anything stored here can be found and removed on its own afterwards.

    Returns ``chunks: 0`` with ``duplicate: true`` when every chunk was already stored
    under that same source. That is a distinct outcome from a failure and the caller
    should say so rather than reporting a successful save of nothing.
    """
    routes_instances._check_instance_name(instance)   # ingest can CREATE an instance
    text   = str(payload.get("text") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if not source:
        # Chunks with no source are unattributable AND undeletable: the per-document
        # delete matches on exactly this value, so there would be no way to take them
        # back out short of resetting the whole instance.
        raise HTTPException(400, "source is required")
    if len(source) > _MAX_SOURCE_LEN:
        raise HTTPException(400, f"source must be at most {_MAX_SOURCE_LEN} characters")
    size = len(text.encode("utf-8"))
    if size > config._MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"text is {size // (1024 * 1024)} MB; the limit is "
                 f"{config._MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                 f"(raise REDIRECALL_MAX_UPLOAD_MB to change it)")

    rc = rag_admin._rc_for(instance, endpoint)
    chunks = await asyncio.to_thread(ingest.ingest_text, instance, text, source, rc)
    config.append_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance": instance, "source": source, "chunks": chunks,
        "status": "ok" if chunks else "skipped",
        "error": "" if chunks else "already stored under this source",
    })
    return {"ok": True, "instance": instance, "source": source,
            "chunks": chunks, "duplicate": chunks == 0}


@appcore.app.get("/api/ingest/active")
async def api_ingest_active():
    """Return the state of all running (and recently finished) file-ingest jobs."""
    _ns_state.reap_finished_ingests()   # expire phantoms before anyone attaches to one
    return [{k: v for k, v in st.items() if k not in _INGEST_PRIVATE_FIELDS}
            for st in _ns_state._active_ingests.values()]


@appcore.app.post("/api/ingest/cancel")
async def api_ingest_cancel(payload: dict):
    """Stop a running file ingest before its next file.

    Files already indexed stay indexed — this is the same between-items stop the crawl
    pause uses, not a rollback. Uploads that were never reached are deleted.
    """
    job = str(payload.get("job", ""))
    ev = _ns_state._ingest_cancels.get(job)
    if ev is None:
        raise HTTPException(404, "No active ingest with that id")
    ev.set()
    st = _ns_state._active_ingests.get(job)
    if st is not None:
        st["cancelled"] = True
    return {"ok": True, "job": job}


@appcore.app.post("/api/crawl/pause")
async def api_crawl_pause(payload: dict):
    """Pause or resume a running crawl without discarding its progress.

    Cancelling ends the task; pausing only stops workers picking up the next page,
    so already-indexed chunks, the visited set and the queue survive.
    """
    # The crawler keys its gate on the fragment-stripped seed URL, so pausing
    # "https://x/#intro" looked up a key nobody had registered: it returned
    # {"paused": true} while the crawl ran on untouched.
    url    = crawler._strip_fragment(payload.get("url", ""))
    paused = bool(payload.get("paused", True))
    gate   = _ns_state._crawl_gates.get(url)
    if gate is None:
        raise HTTPException(404, "No active crawl for that URL")
    if paused:
        gate.clear()
    else:
        gate.set()
    state = _ns_state._active_crawls.get(url)
    if state is not None:
        state["paused"] = paused
    return {"ok": True, "url": url, "paused": paused}


@appcore.app.post("/api/crawl/cancel")
async def api_crawl_cancel(payload: dict):
    """Cancel a running crawl by seed URL."""
    url  = crawler._strip_fragment(payload.get("url", ""))   # keyed as the crawler keys it
    g = _ns_state._crawl_gates.get(url)
    if g is not None:
        g.set()          # release paused workers so the cancel actually lands
    task = _ns_state._crawl_tasks.get(url)
    if task and not task.done():
        task.cancel()
        # Don't await — return immediately; background task cleans up on its own
    if url in _ns_state._active_crawls:
        _ns_state._active_crawls[url]["done"] = True
    return {"ok": True}


@appcore.app.post("/api/rag/{instance}/ingest/url")
async def api_ingest_url(instance: str, payload: dict, endpoint: str | None = None):
    """Non-streaming URL ingest (waits for full completion before returning)."""
    # Normalise once, here: the crawler strips the fragment from the seed internally, so
    # registering the gate and the crawl state under the raw URL left /api/crawl/pause and
    # /api/crawl/cancel unable to find either.
    url              = crawler._strip_fragment(payload.get("url", ""))
    try:
        crawler.assert_public_url(url)          # SSRF guard on the seed URL
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"URL rejected: {e}")
    depth            = int(payload.get("depth", 0))
    respect_robots   = bool(payload.get("respect_robots", True))
    local_only       = bool(payload.get("local_only", True))
    path_prefix_only = bool(payload.get("path_prefix_only", False))
    force_reindex    = bool(payload.get("force_reindex", False))
    max_pages        = int(payload.get("max_pages", 0))
    _crawl_cfg       = _ns_state._config.get("crawl", {})
    concurrency      = int(payload.get("concurrency",    _crawl_cfg.get("concurrency", 10)))
    js_render        = bool(payload.get("js_render",     _crawl_cfg.get("js_render", False)))
    js_concurrency   = int(payload.get("js_concurrency", _crawl_cfg.get("js_concurrency", 3)))
    smart_mode       = bool(payload.get("smart_mode",    _crawl_cfg.get("smart_mode", True)))
    min_words        = int(payload.get("min_words",      _crawl_cfg.get("min_words", 100)))
    rc               = rag_admin._rc_for(instance, endpoint)
    results: list    = []

    _ns_state._active_crawls[url] = {
        "url": url, "instance": instance,
        "pages_done": 0, "chunks": 0, "errors": 0, "blocked": 0, "skipped": 0,
        # Frontier size, kept current by the crawler itself. pages_done/discovered is the
        # only progress ratio a BFS over an unknown site can honestly offer.
        "discovered": 0, "queued": 0, "resolved": 0, "max_pages": max_pages,
        "start_ts": datetime.now(timezone.utc).isoformat(), "done": False,
        "paused": False,
    }
    gate = asyncio.Event()
    gate.set()                      # starts running; cleared only by /api/crawl/pause
    _ns_state._crawl_gates[url] = gate

    async def cb(u, status, n=0, err="", count=0):
        results.append({"url": u, "status": status, "chunks": n, "error": err, "pages_done": count})
        state = _ns_state._active_crawls.get(url)
        if state:
            state["pages_done"] = count
            if status == "indexed":   state["chunks"]  += n
            elif status == "error":   state["errors"]  += 1
            elif status == "blocked": state["blocked"] += 1
            elif status == "skipped": state["skipped"] += 1

    task = asyncio.create_task(
        crawler.crawl_url(instance, url, depth, progress_cb=cb,
                  respect_robots=respect_robots, local_only=local_only,
                  path_prefix_only=path_prefix_only,
                  max_pages=max_pages, concurrency=concurrency, js_render=js_render,
                  js_concurrency=js_concurrency, smart_mode=smart_mode,
                  min_words=min_words, rc=rc, force_reindex=force_reindex,
                  stats=_ns_state._active_crawls[url])
    )
    _ns_state.reap_finished_crawls()          # drop tasks left by earlier crawls
    _ns_state._crawl_tasks[url] = task
    try:
        await task
    finally:
        if url in _ns_state._active_crawls:
            _ns_state._active_crawls[url]["done"] = True
        # Release the gate; a paused-then-cancelled crawl must not leak one.
        _ns_state._crawl_gates.pop(url, None)
    return results


@appcore.app.get("/api/rag/{instance}/ingest/url/stream")
async def api_ingest_url_stream(
    instance: str,
    url: str,
    depth: int = 0,
    respect_robots: bool = True,
    local_only: bool = True,
    path_prefix_only: bool = False,
    force_reindex: bool = False,
    max_pages: int = 0,
    concurrency: int = 10,
    js_render: bool = False,
    js_concurrency: int = 3,
    smart_mode: bool = True,
    min_words: int = 100,
    endpoint: str | None = None,
):
    """
    SSE endpoint that streams crawl progress in real-time.
    Each event is a JSON object: {url, status, chunks, error, pages_done}.
    A final {done: true} event signals completion.
    When the client disconnects the server-side crawl task is cancelled.
    """
    queue: asyncio.Queue = asyncio.Queue()
    rc = rag_admin._rc_for(instance, endpoint)
    # Same normalisation as the non-streaming twin — this is the endpoint the UI drives,
    # so a fragment here is what actually left Pause and Cancel keyed to nothing.
    url = crawler._strip_fragment(url)

    try:
        crawler.assert_public_url(url)          # SSRF guard on the seed URL
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"URL rejected: {e}")

    _ns_state._active_crawls[url] = {
        "url": url, "instance": instance,
        "pages_done": 0, "chunks": 0, "errors": 0, "blocked": 0, "skipped": 0,
        # Frontier size, kept current by the crawler itself. pages_done/discovered is the
        # only progress ratio a BFS over an unknown site can honestly offer, and max_pages
        # defaults to 0 (unlimited) — so without these the bar has nothing to divide by.
        "discovered": 0, "queued": 0, "resolved": 0, "max_pages": max_pages,
        "start_ts": datetime.now(timezone.utc).isoformat(), "done": False,
        # Store params so the UI can reconnect with the exact same settings
        "params": {
            "depth": depth, "respect_robots": respect_robots,
            "local_only": local_only, "path_prefix_only": path_prefix_only,
            "force_reindex": force_reindex,
            "max_pages": max_pages, "concurrency": concurrency,
            "js_render": js_render, "js_concurrency": js_concurrency,
            "smart_mode": smart_mode, "min_words": min_words,
        },
        "paused": False,
    }
    # The streaming endpoint is the one the UI drives, so the pause gate has to be
    # registered here too — not only on the non-streaming twin.
    gate = asyncio.Event()
    gate.set()
    _ns_state._crawl_gates[url] = gate

    async def cb(u, status, n=0, err="", count=0):
        state = _ns_state._active_crawls.get(url)
        if state:
            state["pages_done"] = count
            if status == "indexed":   state["chunks"]  += n
            elif status == "error":   state["errors"]  += 1
            elif status == "blocked": state["blocked"] += 1
            elif status == "skipped": state["skipped"] += 1
        await queue.put({"url": u, "status": status, "chunks": n, "error": err,
                         "pages_done": count,
                         "discovered": (state or {}).get("discovered", 0),
                         "queued":     (state or {}).get("queued", 0),
                         "resolved":   (state or {}).get("resolved", 0)})

    async def run():
        try:
            await crawler.crawl_url(
                instance, url, depth, progress_cb=cb,
                respect_robots=respect_robots, local_only=local_only,
                path_prefix_only=path_prefix_only,
                force_reindex=force_reindex,
                max_pages=max_pages, concurrency=concurrency, js_render=js_render,
                js_concurrency=js_concurrency, smart_mode=smart_mode,
                min_words=min_words, rc=rc,
                stats=_ns_state._active_crawls.get(url),
            )
        finally:
            if url in _ns_state._active_crawls:
                _ns_state._active_crawls[url]["done"] = True
            await queue.put(None)   # sentinel: signals the generator to stop

    task = asyncio.create_task(run())
    _ns_state.reap_finished_crawls()          # drop tasks left by earlier crawls
    _ns_state._crawl_tasks[url] = task

    async def generate():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            pass
        finally:
            # Client disconnected — cancel the crawl task so it doesn't keep
            # running and consuming resources with no one listening.
            if not task.done():
                g = _ns_state._crawl_gates.get(url)
                if g is not None:
                    g.set()      # a paused worker would never observe the cancel
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if url in _ns_state._active_crawls:
                _ns_state._active_crawls[url]["done"] = True
            _ns_state._crawl_gates.pop(url, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # identity => GZipMiddleware forwards this SSE stream untouched (gzip buffering
        # would hold trickled progress events until the stream closes).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Content-Encoding": "identity"},
    )


@appcore.app.get("/api/rag/logs")
def api_ingest_logs():
    return _ns_state._ingestion_logs[-200:]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — RAG EXPORT / IMPORT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_EXPORT_BATCH  = 200          # hgetall commands per pipeline call
_EXPORT_BUFSIZE = 256 * 1024  # bytes to accumulate before yielding an NDJSON chunk


def _iter_chunks_pipelined(rc: redis.Redis, prefix: str):
    """
    Yield chunk dicts from Redis efficiently:
    - scan_iter instead of KEYS  → non-blocking cursor scan, safe on large DBs
    - pipelined HGETALL in batches of _EXPORT_BATCH → N/200 round-trips instead of N
    - base64-encode embedding inline (unavoidable, but done once per chunk)
    """
    # One reader for both the full batches and the remainder. They were duplicated blocks
    # that drifted: the remainder read the legacy flat b"embedding" while full batches read
    # the width-named field, so the final partial batch of every export — up to
    # _EXPORT_BATCH-1 chunks — came out with empty vectors, silently and size-dependently.
    field = embeddings.vector_field_for().encode()

    def _drain(keys: list):
        pipe = rc.pipeline(transaction=False)
        for k in keys:
            pipe.hgetall(k)
        for d in pipe.execute():
            if not d:
                continue
            emb_raw = d.get(field) or d.get(b"embedding", b"")   # legacy rows predate the rename
            yield {
                "id":            d.get(b"chunk_id", b"").decode(),
                "text":          d.get(b"text",     b"").decode(),
                "source":        d.get(b"source",   b"").decode(),
                "embedding_b64": base64.b64encode(emb_raw).decode() if emb_raw else "",
            }

    batch: list = []
    for key in rc.scan_iter(rag.chunk_glob_for_prefix(prefix), count=500):
        batch.append(key)
        if len(batch) >= _EXPORT_BATCH:
            yield from _drain(batch)
            batch = []
    if batch:
        yield from _drain(batch)


@appcore.app.get("/api/rag/{instance}/export")
def api_export_rag(instance: str, endpoint: str | None = None):
    """
    Export a RAG instance as a ZIP file (ZIP_STORED — no compression).

    Embeddings are float32 random bytes that compress < 1%; using DEFLATE
    wastes CPU for almost no size gain.  ZIP_STORED skips compression and
    lets the data flow straight to the client, cutting export time by 50-80%.
    """
    rc     = rag_admin._rc_for(instance, endpoint)
    prefix = rag.rag_prefix(instance)
    chunks = list(_iter_chunks_pipelined(rc, prefix))
    meta_raw = rc.get(f"rag_meta:{instance}")
    meta     = json.loads(meta_raw) if meta_raw else {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("meta.json",   json.dumps(meta))
        zf.writestr("chunks.json", json.dumps(chunks))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={instance}_rag.zip"},
    )


@appcore.app.get("/api/rag/{instance}/export/stream")
def api_export_rag_stream(instance: str, endpoint: str | None = None):
    """
    Stream-export a RAG instance as NDJSON (one JSON object per line).
    Each line has a ``_t`` discriminator: "meta" | "chunk" | "done".

    Optimisations vs the naïve approach:
    - scan_iter + pipelined HGETALL  → far fewer Redis round-trips
    - output buffered to _EXPORT_BUFSIZE before yielding → fewer HTTP frames
    """
    rc     = rag_admin._rc_for(instance, endpoint)
    prefix = rag.rag_prefix(instance)

    def generate():
        meta_raw = rc.get(f"rag_meta:{instance}")
        meta = json.loads(meta_raw) if meta_raw else {}
        yield json.dumps({"_t": "meta", **meta}) + "\n"

        count    = 0
        out_buf: list[str] = []
        out_size = 0

        for chunk in _iter_chunks_pipelined(rc, prefix):
            line = json.dumps({"_t": "chunk", **chunk}) + "\n"
            out_buf.append(line)
            out_size += len(line)
            count += 1
            if out_size >= _EXPORT_BUFSIZE:
                yield "".join(out_buf)
                out_buf  = []
                out_size = 0

        if out_buf:
            yield "".join(out_buf)

        yield json.dumps({"_t": "done", "total": count}) + "\n"

    fname = f"{instance}_rag.jsonl"
    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename={fname}",
            "Cache-Control": "no-cache",
        },
    )


@appcore.app.post("/api/rag/{instance}/import")
async def api_import_rag(instance: str, file: UploadFile = File(...), endpoint: str | None = None):
    routes_instances._check_instance_name(instance)   # import can CREATE an instance
    """Import a RAG ZIP or NDJSON (jsonl) export. Re-uses stored embeddings when present."""
    content = await file.read()
    fname   = (file.filename or "").lower()

    # ── NDJSON / jsonl format ──────────────────────────────────────────────────
    if fname.endswith(".jsonl") or fname.endswith(".ndjson"):
        meta: dict      = {}
        chunks_raw: list[dict] = []
        for raw_line in content.decode().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            t = obj.pop("_t", None)
            if t == "meta":
                meta = obj
            elif t == "chunk":
                chunks_raw.append(obj)
            # "done" line is informational only
    # ── ZIP format (legacy) ───────────────────────────────────────────────────
    else:
        buf = io.BytesIO(content)
        with zipfile.ZipFile(buf) as zf:
            meta       = json.loads(zf.read("meta.json"))
            chunks_raw = json.loads(zf.read("chunks.json"))

    # Always write to the instance's CURRENT assigned endpoint, not the one
    # recorded in the ZIP (which may be from a different server entirely).
    rc = rag_admin._rc_for(instance, endpoint)

    prefix = rag.rag_prefix(instance)

    def _do_import():
        # The whole import — read existing meta, write meta, build the index, embed and
        # pipeline the chunks, sync the counter — is synchronous Redis/CPU work. Run it in
        # one thread so a large import never blocks the event loop (nor other sessions).
        existing_meta_raw = rc.get(f"rag_meta:{instance}")
        if existing_meta_raw:
            existing_meta = json.loads(existing_meta_raw)
            meta["redis_endpoint"] = existing_meta.get("redis_endpoint", "default")
            meta.setdefault("color",   existing_meta.get("color",   "#6366f1"))
            meta.setdefault("enabled", existing_meta.get("enabled", True))
        rc.set(f"rag_meta:{instance}", json.dumps(meta))

        rag.ensure_rag_index(instance, rc)
        # An exported vector carries the width of whatever model was active when it was
        # written. Importing it into an index built for a DIFFERENT width is silently
        # rejected by RediSearch, which reproduces exactly the symptom the field-name fix
        # above removed: every chunk present, nothing findable. Re-embed the mismatched
        # ones from their text instead — slower, but the instance actually works.
        vec_field = embeddings.vector_field_for()
        # Read the width from the LOADED model, the same source _get_rag_index builds the
        # index from — the registry reports 0 dims for a custom model, which would silently
        # switch this check off for exactly the setups most likely to hit a mismatch.
        try:
            expect_bytes = embeddings.get_embed_model().get_sentence_embedding_dimension() * 4
        except Exception:
            expect_bytes = 0        # cannot determine the width: import as-is rather than guess
        reembedded = 0
        pipe = rc.pipeline(transaction=False)
        for ch in chunks_raw:
            emb_b64   = ch.get("embedding_b64", "")
            emb_bytes = base64.b64decode(emb_b64) if emb_b64 else b""
            if emb_bytes and expect_bytes and len(emb_bytes) != expect_bytes:
                emb_bytes, reembedded = b"", reembedded + 1
            if not emb_bytes:
                emb_bytes = embeddings.embed(ch["text"]).astype(np.float32).tobytes()
            # The vector MUST go in the width-named field the index was built against
            # (embedding_384 / _768 / _1024) — the same one rag.add_chunks writes. Writing
            # the legacy flat "embedding" stored every imported chunk outside the index, so
            # an imported instance held all its data and returned nothing from any search.
            pipe.hset(f"{prefix}:chunk:{ch['id']}", mapping={
                "text":      ch["text"].encode(),
                "source":    ch.get("source", "").encode(),
                "chunk_id":  str(ch["id"]),
                vec_field:   emb_bytes,
            })
        pipe.execute()
        if reembedded:
            log.warning(f"import into '{instance}': re-embedded {reembedded} chunk(s) whose "
                        f"stored vector did not match the active model's width "
                        f"({expect_bytes // 4} dims)")

        # Sync the chunk counter so future additions don't collide with imported IDs.
        max_id = -1
        for ch in chunks_raw:
            try:
                max_id = max(max_id, int(ch["id"]))
            except (ValueError, TypeError):
                pass
        if max_id >= 0:
            rc.set(f"rag:{instance}:chunk_counter", str(max_id + 1))

    await asyncio.to_thread(_do_import)
    rag_admin.invalidate_rag_meta(instance)   # in-process; the write above has completed

    return {"ok": True, "chunks": len(chunks_raw)}

