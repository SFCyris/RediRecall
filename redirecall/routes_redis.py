# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_redis — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
from datetime import datetime, timezone
import redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import appcore, config, constants, rag, rag_admin, redis_store, routes_settings, state
from . import cache as _ns_cache

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — REDIS ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/redis/endpoints")
def api_list_endpoints():
    """Return all configured Redis endpoints (primary + extras), passwords redacted."""
    def _strip(ep: dict) -> dict:
        e = dict(ep)
        if e.get("password"):
            e["password"] = config._SECRET_SENTINEL
        return e
    primary = _strip({**state._config.get("redis", {}), "name": "default", "primary": True})
    extras  = [_strip(e) for e in state._config.get("redis_endpoints", [])]
    return [primary] + extras


@appcore.app.post("/api/redis/endpoints")
async def api_add_endpoint(payload: dict):
    """
    Add a new named Redis endpoint.
    The name must be unique and must not be 'default'.
    """
    name = payload.get("name", "").strip()
    if not name or name == "default":
        raise HTTPException(400, "Endpoint name must be a non-empty string other than 'default'")
    endpoints = state._config.get("redis_endpoints", [])
    # A sentinel password means "keep the existing one" (the UI never saw it).
    password = payload.get("password", "")
    if password == config._SECRET_SENTINEL:
        existing = next((e for e in endpoints if e.get("name") == name), {})
        password = existing.get("password", "")
    # Upsert: replace if name already exists
    endpoints = [e for e in endpoints if e.get("name") != name]
    endpoints.append({
        "name":     name,
        "host":     payload.get("host", "localhost"),
        "port":     int(payload.get("port", 6379)),
        "db":       int(payload.get("db", 0)),
        "password": password,
        "ssl":      bool(payload.get("ssl", False)),
    })
    state._config["redis_endpoints"] = endpoints
    config.save_config(state._config)
    # Invalidate cached client for this endpoint
    state._redis_clients.pop(name, None)
    return {"ok": True}


@appcore.app.delete("/api/redis/endpoints/{name}")
def api_delete_endpoint(name: str):
    """Remove a named Redis endpoint (cannot delete 'default')."""
    if name == "default":
        raise HTTPException(400, "Cannot delete the default endpoint")
    endpoints = [e for e in state._config.get("redis_endpoints", []) if e.get("name") != name]
    state._config["redis_endpoints"] = endpoints
    config.save_config(state._config)
    state._redis_clients.pop(name, None)
    return {"ok": True}


@appcore.app.get("/api/redis/endpoints/{name}/discover")
def api_discover_endpoint(name: str):
    """
    Scan a named Redis endpoint for existing RAG instances.
    Returns a list of {name, chunks, has_meta} dicts so the UI can prompt
    the user to re-register instances found on that server.
    """
    try:
        rc = redis_store.r_for(name)
        rc.ping()  # fail fast if connection is broken
    except Exception as e:
        raise HTTPException(400, f"Cannot connect to endpoint '{name}': {e}")

    discovered: dict[str, dict] = {}
    try:
        # SCAN can return the same key more than once under concurrent writes, so count
        # DISTINCT chunk keys per instance rather than incrementing on every hit.
        seen_chunks: dict[str, set] = {}
        for k in rc.scan_iter("rag:*:chunk:*", count=500):   # SCAN, not KEYS (O(N) blocks Redis)
            parts = k.decode().split(":")
            if len(parts) >= 3:
                inst = parts[1]
                seen_chunks.setdefault(inst, set()).add(k)
        for inst, keys in seen_chunks.items():
            discovered.setdefault(inst, {"name": inst, "chunks": 0, "has_meta": False})
            discovered[inst]["chunks"] = len(keys)
        for mk in rc.scan_iter("rag_meta:*", count=500):     # SCAN, not KEYS
            inst = mk.decode().replace("rag_meta:", "")
            discovered.setdefault(inst, {"name": inst, "chunks": 0, "has_meta": False})
            discovered[inst]["has_meta"] = True
    except Exception as e:
        raise HTTPException(500, f"Discovery scan failed: {e}")

    return sorted(discovered.values(), key=lambda x: x["name"])


@appcore.app.post("/api/redis/endpoints/{name}/register")
def api_register_discovered(name: str, payload: dict):
    """
    Register a list of discovered RAG instances on a named endpoint.
    Ensures each instance has a rag_meta key that references this endpoint
    and that its search index exists.
    """
    instances = payload.get("instances", [])
    if not instances:
        return {"ok": True, "registered": 0}
    try:
        rc = redis_store.r_for(name)
    except Exception as e:
        raise HTTPException(400, f"Cannot connect to endpoint '{name}': {e}")

    registered = 0
    for inst_name in instances:
        try:
            meta_raw = rc.get(f"rag_meta:{inst_name}")
            meta = json.loads(meta_raw) if meta_raw else {}
            meta["redis_endpoint"] = name
            meta.setdefault("created", datetime.now(timezone.utc).isoformat())
            meta.setdefault("color", "#6366f1")
            meta.setdefault("enabled", True)
            rc.set(f"rag_meta:{inst_name}", json.dumps(meta))
            rag_admin.invalidate_rag_meta(inst_name)
            rag.ensure_rag_index(inst_name, rc)
            registered += 1
        except Exception as e:
            log.warning(f"Failed to register '{inst_name}' on endpoint '{name}': {e}")

    return {"ok": True, "registered": registered}


@appcore.app.post("/api/redis/test")
async def api_test_redis_adhoc(payload: dict):
    """
    Test a Redis connection using parameters supplied in the request body.
    Does NOT use saved config — validates form-field values before saving.
    Body: {host, port, db, password, ssl}
    """
    try:
        host = payload.get("host", "localhost")
        port = int(payload.get("port", 6379))
        password = payload.get("password", "")
        # The UI never receives real passwords (they're redacted to a sentinel).
        # If it echoes the sentinel back, resolve it against the stored config
        # for the matching host:port so "Test" works without re-typing the key.
        if password == config._SECRET_SENTINEL:
            candidates = [state._config.get("redis", {})] + list(state._config.get("redis_endpoints", []))
            match = next(
                (e for e in candidates
                 if e.get("host") == host and int(e.get("port", 6379)) == port),
                {},
            )
            password = match.get("password", "")
        rc = redis.Redis(
            host=host,
            port=port,
            db=int(payload.get("db", 0)),
            password=password or None,
            ssl=bool(payload.get("ssl", False)),
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=False,
        )
        info = await asyncio.to_thread(rc.info)   # keep the sync client off the event loop
        # Use a unique key so the probe result doesn't pollute the saved-endpoint cache
        probe_key = f"__adhoc_{payload.get('host')}:{payload.get('port')}"
        search_ok = await asyncio.to_thread(routes_settings.probe_search, rc, probe_key)
        # Don't persist this ad-hoc probe result permanently
        state._search_available.pop(probe_key, None)
        return {
            "ok":               True,
            "version":          info.get("redis_version"),
            "memory_used":      info.get("used_memory_human"),
            "connected_clients":info.get("connected_clients"),
            "mode":             info.get("redis_mode", "standalone"),
            "search_available": search_ok,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.post("/api/redis/endpoints/{name}/test")
async def api_test_endpoint(name: str):
    """Test connectivity to a named Redis endpoint (uses saved config)."""
    try:
        rc = redis_store.r_for(name)
        info = await asyncio.to_thread(rc.info)
        search_ok = await asyncio.to_thread(routes_settings.probe_search, rc, name)
        return {
            "ok":               True,
            "version":          info.get("redis_version"),
            "memory":           info.get("used_memory_human"),
            "search_available": search_ok,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — CACHE ANALYTICS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/cache/stats")
def api_cache_stats():
    """
    Return a summary of entries in the SemanticCache.
    redisvl stores the prompt in a field named 'prompt' (previously 'query').
    """
    try:
        rc   = redis_store.r()
        keys = list(rc.scan_iter(f"{constants.CACHE_PREFIX}*", count=200))
        entries = []
        pipe = rc.pipeline(transaction=False)
        for k in keys:
            pipe.hget(k, "prompt")
            pipe.ttl(k)
        results = pipe.execute()
        for i in range(0, len(results), 2):
            prompt_raw = results[i]
            ttl        = results[i + 1]
            prompt     = prompt_raw.decode() if isinstance(prompt_raw, bytes) else (prompt_raw or "")
            entries.append({"query": prompt, "ttl": ttl})
        return {"count": len(keys), "entries": entries}
    except Exception as e:
        return {"count": 0, "entries": [], "error": str(e)}


@appcore.app.delete("/api/cache")
def api_cache_clear():
    """Clear all semantic cache entries. Uses SemanticCache.clear() when available."""
    cache = _ns_cache._get_semantic_cache()
    if cache is not None:
        try:
            cache.clear()
            return {"deleted": "all"}
        except Exception:
            pass  # fall through to manual key deletion
    rc = redis_store.r()
    deleted = 0
    batch: list = []
    for k in rc.scan_iter(f"{constants.CACHE_PREFIX}*", count=500):   # SCAN, not KEYS (O(N) blocks Redis)
        batch.append(k)
        if len(batch) >= 500:
            rc.delete(*batch); deleted += len(batch); batch = []
    if batch:
        rc.delete(*batch); deleted += len(batch)
    state._semantic_cache = None  # reset so it re-indexes on next use
    return {"deleted": deleted}


@appcore.app.delete("/api/cache/entry")
def api_delete_cache_entry(entry_id: str):
    """
    Delete a single cache entry by its entry_id.
    redisvl SemanticCache stores entries as HASH keys named "{name}:{entry_id}".
    """
    if not entry_id:
        return {"ok": False, "error": "entry_id required"}
    try:
        # SemanticCache name is CACHE_PREFIX stripped of the trailing ":"
        key = f"{constants.CACHE_PREFIX.rstrip(':')}:{entry_id}"
        deleted = redis_store.r().delete(key)
        return {"ok": bool(deleted)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.get("/api/rag/stats")
def api_rag_stats():
    """
    Return per-instance query statistics accumulated since the server started.

    Metrics:
      queries            — total number of times this instance was searched
      hits               — queries that returned ≥1 chunk above the threshold
      hit_rate           — hits / queries  (0.0–1.0)
      avg_top_score      — mean cosine similarity of the top-1 chunk on hit queries.
                           Serves as an accuracy proxy: higher = better semantic match.
      avg_best_raw_score — mean cosine similarity of the top-1 KNN result *before*
                           threshold filtering, across all queries.  If this is high
                           but hit_rate is low, your similarity_threshold is too strict.
      avg_chunks         — mean number of chunks returned per query (including misses)
    """
    result = []
    for inst, s in state._rag_stats.items():
        q = s["queries"]
        h = s["hits"]
        result.append({
            "name":               inst,
            "queries":            q,
            "hits":               h,
            "misses":             q - h,
            "hit_rate":           round(h / q, 4)                          if q else 0.0,
            "avg_top_score":      round(s["score_sum"] / h, 4)             if h else 0.0,
            "avg_best_raw_score": round(s.get("raw_score_sum", 0.0) / q, 4) if q else 0.0,
            "avg_chunks":         round(s["chunks_total"] / q, 2)          if q else 0.0,
        })
    result.sort(key=lambda x: x["queries"], reverse=True)
    return result


@appcore.app.delete("/api/rag/stats")
def api_rag_stats_reset():
    """Reset all per-instance RAG statistics."""
    state._rag_stats.clear()
    return {"ok": True}


