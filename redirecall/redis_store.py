# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.redis_store — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import redis
from . import rag, rag_admin, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REDIS HELPERS — multi-endpoint aware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_redis_client(cfg: dict) -> redis.Redis:
    """Build a redis.Redis client from a connection-config dict."""
    return redis.Redis(
        host=cfg.get("host") or "localhost",
        port=int(cfg.get("port") or 6379),    # or-default handles None (JSON null) safely
        db=int(cfg.get("db") or 0),
        password=cfg.get("password") or None,
        ssl=bool(cfg.get("ssl") or False),
        decode_responses=False,   # we handle bytes manually for embedding binary data
        socket_connect_timeout=5,  # fail fast if endpoint is unreachable
        socket_timeout=10,         # prevent blocking forever on slow endpoints
    )


def get_redis(cfg: dict | None = None) -> redis.Redis:
    """
    Return (and cache) the primary Redis client.
    Pass cfg to temporarily use a different connection config (e.g. for test).
    """
    c = cfg or state._config.get("redis", {})
    client = _build_redis_client(c)
    state._redis_clients["default"] = client
    return client


def r_for(endpoint_name: str = "default") -> redis.Redis:
    """
    Return the Redis client for a named endpoint.
    "default" maps to the primary redis config.
    Other names are looked up in config["redis_endpoints"].
    Clients are cached so we reuse connections.
    """
    if endpoint_name in state._redis_clients:
        return state._redis_clients[endpoint_name]

    if endpoint_name == "default":
        return get_redis()

    # Find the named endpoint in config
    for ep in state._config.get("redis_endpoints", []):
        if ep.get("name") == endpoint_name:
            client = _build_redis_client(ep)
            state._redis_clients[endpoint_name] = client
            return client

    # Fallback to default if not found
    log.warning(f"Redis endpoint '{endpoint_name}' not found — falling back to default")
    return r()


def r() -> redis.Redis:
    """Shorthand: return the default Redis client, creating it if needed."""
    if "default" not in state._redis_clients:
        return get_redis()
    return state._redis_clients["default"]


async def _reset_index_markers_for_embedding_change():
    """Drop every index schema marker so all indexes rebuild at the new dimension.

    Shared by the Settings save guard and by config import — a model change through
    either route leaves every stored vector at the old dimension, which makes
    retrieval return nothing at all rather than failing loudly.
    """
    for inst in await asyncio.to_thread(rag_admin.list_rag_instances):
        name = inst.get("name", "")
        if not name:
            continue
        try:
            rc_i = rag_admin.rc_for_instance(name)
            await asyncio.to_thread(rc_i.delete, f"rag:{name}:schema_ver")
            rag._index_ensured.discard(name)
        except Exception as e:
            log.warning(f"  could not reset index marker for '{name}': {e}")
    rag._dim_mismatch_warned.clear()   # allow the diagnostic to fire again if needed


def invalidate_redis_clients():
    """
    Clear the client cache so they're rebuilt on next use.
    Also resets the SemanticCache so it reconnects with the new client.
    Called after config changes that affect Redis connections.
    """
    state._redis_clients.clear()
    state._semantic_cache = None


def _probe_endpoint(ep_name: str) -> bool:
    """
    Return True if the named Redis endpoint responds to PING within the
    socket_connect_timeout.  Updates _endpoint_health in place.
    Never raises.
    """
    try:
        rc = r_for(ep_name)
        rc.ping()
        state._endpoint_health[ep_name] = True
        return True
    except Exception:
        state._endpoint_health[ep_name] = False
        log.warning(f"Redis endpoint '{ep_name}' is unreachable — RAG instances on it will be marked offline")
        return False


def refresh_endpoint_health() -> dict[str, bool]:
    """
    Probe all configured Redis endpoints (default + extras) and return the
    updated health dict.  Called at startup and periodically in background.
    """
    _probe_endpoint("default")
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            _probe_endpoint(ep_name)
    return dict(state._endpoint_health)

