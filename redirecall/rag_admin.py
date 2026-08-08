# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.rag_admin — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
import redis
from . import config, rag, redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAG INSTANCE MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_rag_instances() -> list[dict]:
    """
    Return metadata for all RAG instances across all configured Redis endpoints.

    Each instance can live on a different endpoint (stored in its rag_meta).
    We scan every endpoint so the UI shows a unified list.
    """
    all_instances: dict[str, dict] = {}   # "endpoint:name" -> info

    # Build the list of endpoints to scan: default + any extras in config
    endpoints_to_scan = [("default", redis_store.r())]
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            try:
                endpoints_to_scan.append((ep_name, redis_store.r_for(ep_name)))
            except Exception:
                pass

    for ep_name, rc in endpoints_to_scan:
        # Skip endpoints already known to be down — avoids hanging on socket_timeout
        if state._endpoint_health.get(ep_name, None) is False:
            continue
        try:
            # Enumerate this endpoint's RAG indexes (FT._LIST) and read each
            # instance's chunk count directly from the index (FT.SEARCH … LIMIT 0 0
            # returns the match total). O(instances) index reads instead of a scan
            # over every chunk key in the keyspace.
            try:
                idx_names = [rag._decode(x) for x in rc.execute_command("FT._LIST")]
            except Exception:
                idx_names = []
            for idx_name in idx_names:
                if not (idx_name.startswith("rag:") and idx_name.endswith(":idx")):
                    continue
                inst = idx_name[len("rag:"):-len(":idx")]
                if not inst:
                    continue
                try:
                    res = rc.execute_command("FT.SEARCH", idx_name, "*", "LIMIT", "0", "0")
                    count = int(res[0]) if res else 0
                except Exception:
                    count = 0
                key = f"{ep_name}:{inst}"
                all_instances[key] = {"count": count, "ep": ep_name, "name": inst}
            # Pick up instances that exist only as metadata — created but never
            # ingested, so they have no index yet (rag_meta keys are few and cheap).
            for mk in rc.scan_iter("rag_meta:*", count=200):
                inst = rag._decode(mk).replace("rag_meta:", "")
                key  = f"{ep_name}:{inst}"
                all_instances.setdefault(key, {"count": 0, "ep": ep_name, "name": inst})
        except Exception:
            pass

    # Pipeline metadata fetches grouped by endpoint to avoid N round-trips
    ep_instances: dict[str, list[tuple[str, str]]] = {}  # ep_name → [(key, inst)]
    for key, info in all_instances.items():
        ep_instances.setdefault(info["ep"], []).append((key, info["name"]))

    meta_cache: dict[str, dict] = {}
    for ep_name, pairs in ep_instances.items():
        try:
            rc = redis_store.r_for(ep_name)
            pipe = rc.pipeline(transaction=False)
            for _, inst in pairs:
                pipe.get(f"rag_meta:{inst}")
            raws = pipe.execute()
            for (key, _), raw in zip(pairs, raws):
                meta_cache[key] = json.loads(raw) if raw else {}
        except Exception:
            for key, _ in pairs:
                meta_cache[key] = {}

    result = []
    for key, info in all_instances.items():
        ep_name, inst = info["ep"], info["name"]
        meta = meta_cache.get(key, {})
        resolved_ep = meta.get("redis_endpoint", ep_name)
        # Unknown endpoints (not yet probed) are treated as reachable
        reachable = state._endpoint_health.get(resolved_ep, True)
        result.append({
            "name":            inst,
            "chunks":          info["count"],
            "color":           meta.get("color",   "#6366f1"),
            "tags":            meta.get("tags",    []),
            "created":         meta.get("created", ""),
            "enabled":         meta.get("enabled", True),
            "redis_endpoint":  resolved_ep,
            "reachable":       reachable,
        })
    return result


def reset_rag(instance: str, rc: redis.Redis | None = None):
    """
    Delete all chunk keys and the FT index for a RAG instance.

    Uses FT.DROPINDEX with the DD (Delete Documents) flag which atomically
    drops the index AND removes all indexed HASH keys in a single command —
    far more efficient than the previous KEYS scan + bulk DELETE approach.
    The chunk counter key is cleaned up separately.
    """
    rc = rc or redis_store.r()
    prefix = rag.rag_prefix(instance)
    rag._index_ensured.discard(instance)   # force re-creation on next ingest
    rag._bm25_leg_warned.discard(instance)  # re-warn if the rebuilt index still has no text leg
    rag._emb_mismatch_warned.discard(instance)
    rag._dim_mismatch_warned.discard(instance)  # same: re-arm the dimension diagnostic
    try:
        rc.execute_command("FT.DROPINDEX", f"{prefix}:idx", "DD")
    except Exception:
        pass  # index may not exist yet — that's fine
    # Belt-and-suspenders: explicitly delete any remaining chunk HASH keys.
    # FT.DROPINDEX DD may silently fail (e.g. index never existed), leaving
    # chunk keys behind so the instance keeps reappearing in list scans.
    batch: list = []
    for k in rc.scan_iter(f"{prefix}:chunk:*", count=500):
        batch.append(k)
        if len(batch) >= 500:
            rc.delete(*batch)
            batch = []
    if batch:
        rc.delete(*batch)
    # Remove counter, chunk hash dedup set, URL skip list, and the schema-version
    # marker in one call — so a reset/delete leaves no orphan keys behind.
    rc.delete(f"rag:{instance}:chunk_counter", f"rag:{instance}:chunk_hashes",
              f"rag:{instance}:indexed_urls", f"rag:{instance}:schema_ver")
    config.append_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance": instance,
        "source": "RESET",
        "chunks": 0,
        "status": "reset",
    })


# ── rag_meta read-through cache ──────────────────────────────────────────────
# rag_meta:{instance} = {"enabled": bool, "redis_endpoint": str, ...}. It is read on
# every chat turn — to pick the owning endpoint and check the enabled flag — but written
# only when an instance is created, toggled or deleted. Reading it fresh each turn cost
# up to two Redis round trips per instance (rc_for_instance re-read the same key), which
# is pure latency against a remote Redis. This short-TTL in-process cache turns the hot
# path into a dict lookup; writes invalidate explicitly, so the TTL only bounds staleness
# in the (unexpected) event an invalidation is ever missed.
_RAG_META_TTL = 3.0                       # seconds
_rag_meta_cache: dict[str, tuple[float, tuple[dict | None, str]]] = {}
# The cache is read from the event loop AND from to_thread / FastAPI-threadpool workers,
# and writes to rag_meta invalidate it. A generation counter guarded by a lock closes the
# resolve-then-store race: if an invalidation lands while a resolve is in flight, the stale
# value is not cached (we re-resolve on the next read instead).
_rag_meta_lock = threading.Lock()

def _resolve_rag_meta(instance: str) -> tuple[dict | None, str]:
    """Find an instance's rag_meta across endpoints. Returns (meta_or_None, endpoint_name).
    One GET on the default endpoint in the common case; extra endpoints are consulted only
    when the instance is not on the default. Mirrors the old rc_for_instance search order."""
    try:
        meta_raw = redis_store.r().get(f"rag_meta:{instance}")
        if meta_raw:
            meta = json.loads(meta_raw)
            return meta, meta.get("redis_endpoint", "default")
    except Exception:
        pass
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            try:
                meta_raw = redis_store.r_for(ep_name).get(f"rag_meta:{instance}")
                if meta_raw:
                    return json.loads(meta_raw), ep_name
            except Exception:
                pass
    return None, "default"

def _rag_meta_cached(instance: str) -> tuple[dict | None, str]:
    """Cached (meta, endpoint) for an instance. Cache hit performs no Redis I/O."""
    ent = _rag_meta_cache.get(instance)
    if ent and (time.time() - ent[0]) < _RAG_META_TTL:
        return ent[1]
    with _rag_meta_lock:
        gen_before = state._rag_meta_gen
    val = _resolve_rag_meta(instance)                 # Redis I/O outside the lock
    with _rag_meta_lock:
        if state._rag_meta_gen == gen_before:               # no invalidation raced us
            _rag_meta_cache[instance] = (time.time(), val)
    return val

async def _rag_meta_cached_async(instance: str) -> tuple[dict | None, str]:
    """Same as _rag_meta_cached, but a cache miss resolves off the event loop."""
    ent = _rag_meta_cache.get(instance)
    if ent and (time.time() - ent[0]) < _RAG_META_TTL:
        return ent[1]
    return await asyncio.to_thread(_rag_meta_cached, instance)

def invalidate_rag_meta(instance: str | None = None):
    """Drop cached rag_meta after a write so the next read reflects it immediately."""
    with _rag_meta_lock:
        state._rag_meta_gen += 1                             # cancels any in-flight resolve's store
        if instance is None:
            _rag_meta_cache.clear()
        else:
            _rag_meta_cache.pop(instance, None)

def rc_for_instance(instance: str) -> redis.Redis:
    """
    Return the Redis client that owns a specific RAG instance.
    Endpoint is resolved via the cached rag_meta (default endpoint first, then extras),
    falling back to the default client if the instance is not found anywhere.
    """
    _meta, ep = _rag_meta_cached(instance)
    return redis_store.r_for(ep)

def _rc_for(instance: str, endpoint: str | None = None) -> redis.Redis:
    """
    Return the Redis client for an instance.
    When ``endpoint`` is supplied explicitly (e.g. from a query parameter) it
    is used directly, bypassing the metadata lookup.  This lets callers target
    the correct server even when two instances share the same name on different
    endpoints.
    """
    if endpoint:
        return redis_store.r_for(endpoint)
    return rc_for_instance(instance)

