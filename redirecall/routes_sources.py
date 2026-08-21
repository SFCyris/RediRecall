# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_sources — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import hashlib
import time
from datetime import datetime, timezone
import redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import appcore, config, crawler, rag, rag_admin, routes_ingestion, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — RAG SOURCE LISTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scan_unique_sources(rc: redis.Redis, prefix: str) -> set[str]:
    """Fallback: derive the distinct source set by scanning chunk hashes.
    Used only when the index is unavailable for FT.AGGREGATE."""
    sources: set[str] = set()
    batch: list = []
    def _drain(keys):
        pipe = rc.pipeline(transaction=False)
        for k in keys:
            pipe.hget(k, "source")
        for raw in pipe.execute():
            if raw:
                src = raw.decode() if isinstance(raw, bytes) else raw
                if src:
                    sources.add(src)
    for key in rc.scan_iter(rag.chunk_glob_for_prefix(prefix), count=500):
        batch.append(key)
        if len(batch) >= routes_ingestion._EXPORT_BATCH:
            _drain(batch); batch = []
    if batch:
        _drain(batch)
    return sources


@appcore.app.get("/api/rag/{instance}/sources")
def api_rag_sources(instance: str, endpoint: str | None = None):
    """Return all unique source identifiers stored in a RAG instance."""
    rc     = rag_admin._rc_for(instance, endpoint)
    prefix = rag.rag_prefix(instance)
    idx_name = f"{prefix}:idx"
    # One server-side aggregation over the index: GROUPBY the source field returns
    # each distinct source once — instead of scanning + HGET-ing every chunk.
    try:
        rows = rag.aggregate_all(rc, idx_name, "GROUPBY", "1", "@source")
        sources: set[str] = set()
        for row in rows:
            for j in range(0, len(row) - 1, 2):
                if rag._decode(row[j]) == "source":
                    val = rag._decode(row[j + 1])
                    if val:
                        sources.add(val)
        return sorted(sources)
    except Exception as e:
        log.warning(f"api_rag_sources: aggregate failed for '{instance}', scanning instead: {e}")
        return sorted(_scan_unique_sources(rc, prefix))


@appcore.app.get("/api/rag/{instance}/documents")
def api_rag_documents(instance: str, endpoint: str | None = None):
    """List the documents in an instance with per-document chunk counts.

    Richer counterpart to /sources (which stays a plain list for compatibility).
    One FT.AGGREGATE groups by source and counts — no keyspace scan.
    """
    rc = rag_admin._rc_for(instance, endpoint)
    idx_name = f"{rag.rag_prefix(instance)}:idx"
    try:
        rows = rag.aggregate_all(
            rc, idx_name,
            "GROUPBY", "1", "@source",
            "REDUCE", "COUNT", "0", "AS", "chunks",
            "REDUCE", "MAX", "1", "@ingested_at", "AS", "ingested_at",
        )
        docs = []
        for row in rows:
            d = {rag._decode(row[j]): rag._decode(row[j + 1]) for j in range(0, len(row) - 1, 2)}
            src = d.get("source", "")
            if not src:
                continue
            try:
                ingested = int(float(d.get("ingested_at") or 0))
            except Exception:
                ingested = 0
            docs.append({
                "source": src,
                "doc_id": rag.doc_id_for(src),
                "chunks": int(d.get("chunks") or 0),
                # 0 for documents ingested before the v3 schema added the field.
                "ingested_at": ingested,
            })
        docs.sort(key=lambda x: x["source"])
        return {"total": len(docs), "documents": docs}
    except Exception as e:
        log.warning(f"api_rag_documents failed for '{instance}': {e}")
        raise HTTPException(500, f"Could not list documents: {e}")


@appcore.app.delete("/api/rag/{instance}/documents")
def api_delete_document(instance: str, source: str, endpoint: str | None = None):
    """Delete every chunk belonging to ONE source document.

    Previously the only removal options were "wipe all chunks" or "delete the
    instance", so a single stale document could not be replaced without
    re-ingesting everything. Chunks are located through the index (exact match on
    the source TAG) rather than by scanning the keyspace, and their dedup hashes
    are released so the same document can be re-ingested afterwards.
    """
    if not source:
        raise HTTPException(400, "source is required")
    rc       = rag_admin._rc_for(instance, endpoint)
    prefix   = rag.rag_prefix(instance)
    idx_name = f"{prefix}:idx"
    esc      = rag._TAG_ESCAPE_RE.sub(r"\\\1", source)
    deleted, hashes = 0, []
    try:
        offset = 0
        for _batch in range(rag._MAX_DELETE_BATCHES):
            res = rc.execute_command(
                "FT.SEARCH", idx_name, f"@source:{{{esc}}}",
                "RETURN", "1", "text",
                "LIMIT", str(offset), "200",
                "DIALECT", "2",
            )
            items = res[1:]
            if not items:
                break
            pipe = rc.pipeline(transaction=False)
            for i in range(0, len(items), 2):
                key = rag._decode(items[i])
                fields = items[i + 1]
                fmap = {rag._decode(fields[j]): rag._decode(fields[j + 1]) for j in range(0, len(fields) - 1, 2)}
                txt = fmap.get("text", "")
                if txt:
                    # MUST mirror ingest_text's dedup key exactly: hashes are
                    # source-scoped (sha256("{source}\x00{normalised}")). Releasing
                    # the unscoped spelling freed nothing, so a delete + re-ingest
                    # of the same document silently produced 0 chunks — the doc
                    # was gone for good (hit hardest by the watched-folder
                    # change path, which does exactly delete-then-re-ingest).
                    hashes.append(hashlib.sha256(
                        f"{source}\x00{' '.join(txt.lower().split())}".encode()).hexdigest())
                pipe.delete(key)
                deleted += 1
            pipe.execute()
            if len(items) < 400:      # (key, fields) pairs → fewer than 200 docs
                break
        # Release the dedup hashes, otherwise re-ingesting this document would be
        # silently skipped as "duplicate content".
        if hashes:
            hs = f"rag:{instance}:chunk_hashes"
            for i in range(0, len(hashes), 500):
                rc.srem(hs, *hashes[i:i + 500])
        # Crawled pages are gated by a SECOND dedup set. Without releasing it the
        # page is skipped as "already indexed" forever and only a full
        # force-reindex brings it back — the workflow this feature replaces.
        try:
            rc.srem(f"rag:{instance}:indexed_urls", source)
        except Exception:
            pass
        # The chunks are already gone; a disk error while journalling must not
        # turn a successful delete into a 500 that tells the user it failed.
        try:
            config.append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "instance": instance, "source": source,
                "chunks": -deleted, "status": "deleted",
            })
        except Exception as le:
            log.warning(f"delete succeeded but could not be logged: {le}")
        return {"ok": True, "source": source, "deleted": deleted}
    except Exception as e:
        log.warning(f"api_delete_document failed for '{instance}' / {source!r}: {e}")
        raise HTTPException(500, f"Could not delete document: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — SCHEDULED RECRAWL MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/recrawl/sources")
def api_list_scheduled_sources():
    """List all scheduled re-crawl sources."""
    return state._config.get("scheduled_sources", [])


@appcore.app.post("/api/recrawl/sources")
async def api_add_scheduled_source(payload: dict):
    """
    Add or update a URL in the re-crawl schedule.

    Body: {url, instance, depth}
    The scheduler will re-crawl this URL into the given RAG instance
    whenever recrawl.enabled is true and the configured interval has elapsed.
    """
    url = payload.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    sources = [s for s in state._config.get("scheduled_sources", []) if s.get("url") != url]
    sources.append({
        "url":          url,
        "instance":     payload.get("instance", "default"),
        "depth":        int(payload.get("depth", 0)),
        "last_crawled": 0,
    })
    state._config["scheduled_sources"] = sources
    config.save_config(state._config)
    return {"ok": True}


@appcore.app.delete("/api/recrawl/sources")
async def api_delete_scheduled_source(url: str):
    """Remove a URL from the re-crawl schedule."""
    state._config["scheduled_sources"] = [
        s for s in state._config.get("scheduled_sources", []) if s.get("url") != url
    ]
    config.save_config(state._config)
    return {"ok": True}


@appcore.app.post("/api/recrawl/trigger")
async def api_trigger_recrawl():
    """Immediately trigger a re-crawl of all scheduled sources (ignores interval)."""
    scheduled = state._config.get("scheduled_sources", [])
    if not scheduled:
        return {"ok": True, "triggered": 0}
    triggered = 0
    now = time.time()
    for src in scheduled:
        url      = src.get("url", "").strip()
        instance = src.get("instance", "default")
        depth    = int(src.get("depth", 0))
        if not url:
            continue
        asyncio.create_task(crawler.crawl_url(instance, url, depth, rc=rag_admin.rc_for_instance(instance)))
        src["last_crawled"] = now
        triggered += 1
    config.save_config(state._config)
    return {"ok": True, "triggered": triggered}


