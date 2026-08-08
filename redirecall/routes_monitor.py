# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_monitor — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import redis
from . import appcore, rag_admin, redis_store, routes_settings, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — REDIS MEMORY MONITOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/redis/memory")
def api_redis_memory():
    try:
        info = redis_store.r().info("memory")
        return {
            "used":       info.get("used_memory", 0),
            "used_human": info.get("used_memory_human", "?"),
            "peak":       info.get("used_memory_peak", 0),
            "peak_human": info.get("used_memory_peak_human", "?"),
            "max":        info.get("maxmemory", 0),
            "max_human":  info.get("maxmemory_human", "nomax"),
        }
    except Exception as e:
        return {"error": str(e)}


def _collect_redis_server_stats(ep_name: str, rc: redis.Redis, ep_cfg: dict) -> dict:
    """
    Collect comprehensive stats for a single Redis endpoint.
    Returns a dict suitable for the analytics UI.
    """
    base: dict = {
        "name":    ep_name,
        "host":    ep_cfg.get("host", "localhost"),
        "port":    int(ep_cfg.get("port", 6379)),
        "db":      int(ep_cfg.get("db", 0)),
        "ok":      False,
    }
    try:
        info = rc.info("all")
    except Exception as e:
        base["error"] = str(e)
        return base

    # ── basic ──────────────────────────────────────────────────────────────────
    base["ok"]      = True
    base["version"] = info.get("redis_version", "?")
    base["mode"]    = info.get("redis_mode", "standalone")
    base["role"]    = info.get("role", "master")
    base["os"]      = info.get("os", "")

    # ── memory ─────────────────────────────────────────────────────────────────
    base["mem_used"]        = info.get("used_memory_human", "?")
    base["mem_used_bytes"]  = info.get("used_memory", 0)
    base["mem_peak"]        = info.get("used_memory_peak_human", "?")
    base["mem_rss"]         = info.get("used_memory_rss_human", "?")
    base["mem_max_bytes"]   = info.get("maxmemory", 0)
    base["mem_max"]         = info.get("maxmemory_human", "0") if info.get("maxmemory") else "unlimited"
    base["mem_pct"]         = (
        round(info.get("used_memory", 0) / info.get("maxmemory") * 100)
        if info.get("maxmemory") else 0
    )
    base["mem_fragmentation_ratio"] = info.get("mem_fragmentation_ratio", 1.0)

    # ── clients & throughput ───────────────────────────────────────────────────
    base["connected_clients"]        = info.get("connected_clients", 0)
    base["blocked_clients"]          = info.get("blocked_clients", 0)
    base["uptime_days"]              = info.get("uptime_in_days", 0)
    base["uptime_seconds"]           = info.get("uptime_in_seconds", 0)
    base["ops_per_sec"]              = info.get("instantaneous_ops_per_sec", 0)
    base["total_commands_processed"] = info.get("total_commands_processed", 0)
    base["total_connections"]        = info.get("total_connections_received", 0)
    base["net_input_bytes"]          = info.get("total_net_input_bytes", 0)
    base["net_output_bytes"]         = info.get("total_net_output_bytes", 0)

    # ── keyspace hit rate ──────────────────────────────────────────────────────
    hits   = info.get("keyspace_hits",   0)
    misses = info.get("keyspace_misses", 0)
    base["keyspace_hits"]   = hits
    base["keyspace_misses"] = misses
    base["keyspace_hit_rate"] = round(hits / (hits + misses) * 100, 1) if (hits + misses) else None

    # ── evictions / expirations ────────────────────────────────────────────────
    base["evicted_keys"] = info.get("evicted_keys", 0)
    base["expired_keys"] = info.get("expired_keys", 0)

    # ── persistence ────────────────────────────────────────────────────────────
    base["rdb_enabled"]            = info.get("rdb_last_bgsave_status") is not None
    base["rdb_last_save_status"]   = info.get("rdb_last_bgsave_status", "?")
    base["rdb_changes_since_save"] = info.get("rdb_changes_since_last_save", 0)
    base["aof_enabled"]            = bool(info.get("aof_enabled", 0))
    base["aof_rewrite_running"]    = bool(info.get("aof_rewrite_in_progress", 0))

    # ── keyspace (databases) ───────────────────────────────────────────────────
    keyspace = []
    total_keys = 0
    for k, v in info.items():
        # Only real per-db entries ("db0", "db1", …) carry {keys, expires, avg_ttl}.
        # Redis 8.8's INFO keyspace section also emits histogram keys such as
        # "db0_distrib_strings_sizes" that start with "db" and parse to a dict —
        # k[2:].isdigit() excludes those so int(k[2:]) below can't blow up.
        if k.startswith("db") and k[2:].isdigit() and isinstance(v, dict):
            keys = v.get("keys", 0)
            total_keys += keys
            keyspace.append({
                "db":      int(k[2:]),
                "keys":    keys,
                "expires": v.get("expires", 0),
                "avg_ttl": v.get("avg_ttl", 0),
            })
    base["keyspace"]   = sorted(keyspace, key=lambda x: x["db"])
    base["total_keys"] = total_keys

    # ── replication ────────────────────────────────────────────────────────────
    base["repl_slaves"]     = info.get("connected_slaves", 0)
    base["repl_master"]     = None
    if info.get("role") == "slave":
        base["repl_master"] = f"{info.get('master_host','?')}:{info.get('master_port','?')}"
    base["repl_backlog"]    = info.get("repl_backlog_active", 0)

    # ── cluster ────────────────────────────────────────────────────────────────
    cluster_enabled = bool(info.get("cluster_enabled", 0))
    base["cluster_enabled"] = cluster_enabled
    if cluster_enabled:
        try:
            ci = rc.execute_command("CLUSTER INFO")
            # CLUSTER INFO returns a bulk string of "key:value\r\n" lines
            if isinstance(ci, (bytes, str)):
                ci_str = ci.decode() if isinstance(ci, bytes) else ci
                ci_map = dict(
                    line.split(":", 1)
                    for line in ci_str.strip().splitlines()
                    if ":" in line
                )
            else:
                ci_map = ci  # some clients parse it to dict already
            base["cluster_state"]       = ci_map.get("cluster_state", "?")
            base["cluster_slots_ok"]    = int(ci_map.get("cluster_slots_ok", 0))
            base["cluster_slots_fail"]  = int(ci_map.get("cluster_slots_fail", 0))
            base["cluster_known_nodes"] = int(ci_map.get("cluster_known_nodes", 0))
            base["cluster_size"]        = int(ci_map.get("cluster_size", 0))
        except Exception:
            base["cluster_state"] = "unknown"
    else:
        base["cluster_state"] = None

    # ── search module ──────────────────────────────────────────────────────────
    base["search_available"] = routes_settings.probe_search(rc, ep_name)

    # ── RAG instances on this endpoint ─────────────────────────────────────────
    all_insts = rag_admin.list_rag_instances()
    base["rag_instances"] = [
        {"name": i["name"], "chunks": i["chunks"], "enabled": i.get("enabled", True)}
        for i in all_insts
        if (i.get("redis_endpoint") or "default") == ep_name
    ]

    return base


@appcore.app.get("/api/redis/all-stats")
def api_redis_all_stats():
    """
    Return comprehensive stats for all configured Redis endpoints
    (default + any extras).  Used by the analytics panel.
    """
    primary_cfg = {**state._config.get("redis", {}), "name": "default"}
    endpoints_to_check = [("default", redis_store.r(), primary_cfg)]
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            try:
                endpoints_to_check.append((ep_name, redis_store.r_for(ep_name), ep))
            except Exception:
                endpoints_to_check.append((ep_name, None, ep))

    results = []
    for ep_name, rc, cfg in endpoints_to_check:
        if rc is None:
            results.append({"name": ep_name, "ok": False, "error": "Could not connect"})
        else:
            results.append(_collect_redis_server_stats(ep_name, rc, cfg))
    return results

