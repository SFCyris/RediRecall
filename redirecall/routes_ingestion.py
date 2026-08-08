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
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from . import appcore, config, constants, crawler, embeddings, ingest, rag, rag_admin
from . import state as _ns_state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — INGESTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/rag/{instance}/ingest/files")
async def api_ingest_files(instance: str, files: list[UploadFile] = File(...), endpoint: str | None = None):
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
    rc = rag_admin._rc_for(instance, endpoint)

    # Buffer all uploads to disk first (so we can stream progress after)
    saved: list[tuple[Path, str]] = []
    for f in files:
        dest, safe_name = constants.safe_upload_dest(f.filename)   # strip path + verify containment
        dest.write_bytes(await f.read())
        saved.append((dest, safe_name))

    async def generate():
        for idx, (path, name) in enumerate(saved):
            try:
                result = await ingest.ingest_file(instance, path, name, rc)
                yield f"data: {json.dumps({'file': name, 'status': result.get('status','ok'), 'chunks': result.get('chunks', 0), 'index': idx, 'total': len(saved)})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'file': name, 'status': 'error', 'error': str(e), 'index': idx, 'total': len(saved)})}\n\n"
            finally:
                # Always remove the uploaded file after indexing — success or
                # failure — to prevent unbounded growth of the uploads directory.
                try:
                    path.unlink(missing_ok=True)
                except Exception as e:
                    log.warning(f"Could not delete upload '{path}': {e}")
        yield f"data: {json.dumps({'done': True, 'total': len(saved)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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

    for k in rc.scan_iter(f"{prefix}:chunk:*", count=500):
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


@appcore.app.post("/api/crawl/pause")
async def api_crawl_pause(payload: dict):
    """Pause or resume a running crawl without discarding its progress.

    Cancelling ends the task; pausing only stops workers picking up the next page,
    so already-indexed chunks, the visited set and the queue survive.
    """
    url    = payload.get("url", "")
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
    url  = payload.get("url", "")
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
    url              = payload.get("url", "")
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
                  min_words=min_words, rc=rc, force_reindex=force_reindex)
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

    try:
        crawler.assert_public_url(url)          # SSRF guard on the seed URL
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"URL rejected: {e}")

    _ns_state._active_crawls[url] = {
        "url": url, "instance": instance,
        "pages_done": 0, "chunks": 0, "errors": 0, "blocked": 0, "skipped": 0,
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
        await queue.put({"url": u, "status": status, "chunks": n, "error": err, "pages_done": count})

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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    batch: list = []
    for key in rc.scan_iter(f"{prefix}:chunk:*", count=500):
        batch.append(key)
        if len(batch) >= _EXPORT_BATCH:
            pipe = rc.pipeline(transaction=False)
            for k in batch:
                pipe.hgetall(k)
            for d in pipe.execute():
                if d:
                    emb_raw = d.get(embeddings.vector_field_for().encode(), b"")
                    yield {
                        "id":            d.get(b"chunk_id", b"").decode(),
                        "text":          d.get(b"text",     b"").decode(),
                        "source":        d.get(b"source",   b"").decode(),
                        "embedding_b64": base64.b64encode(emb_raw).decode() if emb_raw else "",
                    }
            batch = []
    # flush remainder
    if batch:
        pipe = rc.pipeline(transaction=False)
        for k in batch:
            pipe.hgetall(k)
        for d in pipe.execute():
            if d:
                emb_raw = d.get(b"embedding", b"")
                yield {
                    "id":            d.get(b"chunk_id", b"").decode(),
                    "text":          d.get(b"text",     b"").decode(),
                    "source":        d.get(b"source",   b"").decode(),
                    "embedding_b64": base64.b64encode(emb_raw).decode() if emb_raw else "",
                }


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
        pipe = rc.pipeline(transaction=False)
        for ch in chunks_raw:
            emb_b64   = ch.get("embedding_b64", "")
            emb_bytes = (
                base64.b64decode(emb_b64)
                if emb_b64
                else embeddings.embed(ch["text"]).astype(np.float32).tobytes()
            )
            pipe.hset(f"{prefix}:chunk:{ch['id']}", mapping={
                "text":      ch["text"].encode(),
                "source":    ch.get("source", "").encode(),
                "chunk_id":  str(ch["id"]),
                "embedding": emb_bytes,
            })
        pipe.execute()

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

