# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_instances — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import uuid
from datetime import datetime, timezone
from . import appcore, embeddings, rag, rag_admin, redis_store

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — RAG INSTANCES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/embedding/models")
def api_embedding_models():
    """The model registry, plus which models each instance's chunks were built with.

    ``in_use`` is what makes a mixed-model corpus visible: it is the set of
    registry ids actually present in an index, so the UI can show when an
    instance holds vectors from more than one model.
    """
    out = []
    for mid, spec in embeddings.EMBEDDING_MODELS.items():
        out.append({"id": mid, **{k: v for k, v in spec.items()},
                    "field": embeddings.vector_field_for(spec["repo"]),
                    "active": mid == embeddings.embedding_id_for()})
    return {"models": out, "active_id": embeddings.embedding_id_for(),
            "default_id": embeddings.DEFAULT_EMBEDDING_ID}


@appcore.app.get("/api/rag/{instance}/embedding-models")
def api_instance_embedding_models(instance: str, endpoint: str | None = None):
    """Registry ids present in one instance's chunks, with counts."""
    rc = rag_admin._rc_for(instance, endpoint)
    try:
        res = rc.execute_command(
            "FT.AGGREGATE", f"{rag.rag_prefix(instance)}:idx", "*",
            "GROUPBY", "1", "@emb_model",
            "REDUCE", "COUNT", "0", "AS", "n", "LIMIT", "0", "20")
    except Exception as e:
        # Indexes built before the emb_model field exists have no such property.
        # Those chunks predate per-chunk provenance; report the configured model
        # as unverified rather than erroring.
        if "emb_model" in str(e):
            return {"instance": instance, "mixed": False, "legacy": True,
                    "models": [{"id": -1, "chunks": -1,
                                "label": "pre-v4 index — model not recorded per chunk"}]}
        return {"instance": instance, "models": [], "error": str(e)}
    models = []
    for row in res[1:]:
        d = {rag._decode(row[i]): rag._decode(row[i + 1]) for i in range(0, len(row) - 1, 2)}
        try:
            mid = int(d.get("emb_model", -1))
        except (TypeError, ValueError):
            mid = -1
        models.append({
            "id": mid, "chunks": int(d.get("n", 0) or 0),
            "label": embeddings.EMBEDDING_MODELS.get(mid, {}).get(
                "label", "provenance not recorded (ingested before per-chunk tracking)"
                if mid == -1 else "unknown model"),
        })
    return {"instance": instance, "models": sorted(models, key=lambda m: -m["chunks"]),
            "mixed": len(models) > 1}


@appcore.app.get("/api/rag/instances")
def api_rag_instances():
    return rag_admin.list_rag_instances()


@appcore.app.post("/api/rag/instances")
async def api_create_instance(payload: dict):
    """
    Create a new RAG instance with optional metadata.
    Accepts redis_endpoint to store this instance on a specific Redis server.
    """
    name     = payload.get("name", f"rag_{uuid.uuid4().hex[:6]}")
    ep_name  = payload.get("redis_endpoint", "default")
    rc       = redis_store.r_for(ep_name)
    meta = {
        "color":           payload.get("color", "#6366f1"),
        "tags":            payload.get("tags", []),
        "created":         datetime.now(timezone.utc).isoformat(),
        "redis_endpoint":  ep_name,
    }
    await asyncio.to_thread(rc.set, f"rag_meta:{name}", json.dumps(meta))
    rag_admin.invalidate_rag_meta(name)
    await asyncio.to_thread(rag.ensure_rag_index, name, rc)
    return {"name": name, **meta}


@appcore.app.delete("/api/rag/instances/{instance}")
def api_delete_instance(instance: str, endpoint: str | None = None):
    rc = rag_admin._rc_for(instance, endpoint)
    rag_admin.reset_rag(instance, rc)
    rc.delete(f"rag_meta:{instance}")
    rag_admin.invalidate_rag_meta(instance)
    return {"ok": True}


@appcore.app.post("/api/rag/{instance}/toggle")
def api_toggle_rag(instance: str, payload: dict, endpoint: str | None = None):
    """Enable or disable a RAG instance without deleting its data."""
    rc       = rag_admin._rc_for(instance, endpoint)
    meta_raw = rc.get(f"rag_meta:{instance}")
    meta     = json.loads(meta_raw) if meta_raw else {}
    meta["enabled"] = bool(payload.get("enabled", True))
    rc.set(f"rag_meta:{instance}", json.dumps(meta))
    rag_admin.invalidate_rag_meta(instance)
    return {"ok": True, "enabled": meta["enabled"]}


@appcore.app.post("/api/rag/{instance}/reset")
def api_reset_rag(instance: str, endpoint: str | None = None):
    rag_admin.reset_rag(instance, rag_admin._rc_for(instance, endpoint))
    return {"ok": True}


@appcore.app.get("/api/rag/{instance}/chunks")
def api_rag_chunks(instance: str, limit: int = 50, offset: int = 0, endpoint: str | None = None):
    """Return a page of stored chunks for inspection, ordered by chunk_id.

    One FT.SEARCH over the index (SORTBY the sortable chunk_id) returns the
    fields directly and in a deterministic order — instead of a scan whose
    order is arbitrary followed by an HGETALL per key. `offset` enables real
    pagination.
    """
    rc     = rag_admin._rc_for(instance, endpoint)
    prefix = rag.rag_prefix(instance)
    idx_name = f"{prefix}:idx"
    try:
        res = rc.execute_command(
            "FT.SEARCH", idx_name, "*",
            "SORTBY", "chunk_id", "ASC",
            "RETURN", "3", "chunk_id", "text", "source",
            "LIMIT", str(max(0, offset)), str(max(0, limit)),
            "DIALECT", "2",
        )
        chunks = []
        items = res[1:]   # res[0] is the total match count
        for i in range(0, len(items), 2):
            fields = items[i + 1]
            d = {}
            for j in range(0, len(fields), 2):
                d[rag._decode(fields[j])] = rag._decode(fields[j + 1])
            chunks.append({
                "id":     d.get("chunk_id", "0"),
                "text":   d.get("text", "")[:200],
                "source": d.get("source", ""),
            })
        return chunks
    except Exception as e:
        # Index missing → fall back to a bounded scan (arbitrary order). Honour
        # offset by skipping that many keys before collecting up to `limit`.
        log.warning(f"api_rag_chunks: search failed for '{instance}', scanning instead: {e}")
        keys, skipped = [], 0
        for k in rc.scan_iter(f"{prefix}:chunk:*", count=500):
            if skipped < max(0, offset):
                skipped += 1
                continue
            keys.append(k)
            if len(keys) >= limit:
                break
        chunks = []
        for k in keys:
            d = rc.hgetall(k)
            chunks.append({
                "id":     d.get(b"chunk_id", b"0").decode(),
                "text":   d.get(b"text", b"").decode()[:200],
                "source": d.get(b"source", b"").decode(),
            })
        return chunks

