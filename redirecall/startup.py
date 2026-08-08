# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.startup — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import os
import socket
from . import appcore, cache, config, constants, embeddings, rag, rag_admin, redis_store, state, ws

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STARTUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _primary_lan_ip() -> str | None:
    """Best-effort primary LAN IPv4 (the interface used for outbound traffic).
    Uses a UDP socket's chosen source address — no packet is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None
    finally:
        s.close()


@appcore.app.on_event("startup")
async def startup():
    """
    Called once when Uvicorn starts.
    - Loads config from disk (merged with defaults)
    - Reads API keys from environment (they override config-file keys)
    - Connects to Redis and warms up the embedding model
    - Prints diagnostic info to stdout
    """

    state._config = config.load_config()
    config.load_logs()
    config.load_feedback()   # without this the first rating after a restart truncates the store

    log.info("=" * 60)
    log.info(f"RediRecall v{constants.__version__} — startup diagnostics")
    log.info(f"  Config:   {constants.CONFIG_PATH} ({'found' if constants.CONFIG_PATH.exists() else 'NOT FOUND — using defaults'})")
    log.info(f"  Provider: {state._config.get('provider', 'ollama')}")

    # ── Access URLs ────────────────────────────────────────────────────────
    # Show every URL the UI can be reached at, so the user can copy one from the
    # console. Host/port mirror what cli() binds (REDIRECALL_HOST / REDIRECALL_PORT);
    # a raw `uvicorn --host/--port` bypasses these env vars, so the URLs below assume
    # the standard `redirecall` entrypoint.
    _bind_host = os.environ.get("REDIRECALL_HOST", "127.0.0.1")
    _port      = os.environ.get("REDIRECALL_PORT", "8420")
    _in_docker = os.path.exists("/.dockerenv")
    _urls: list[str] = []
    if _bind_host in ("0.0.0.0", "::", ""):
        # Bound to all interfaces — reachable on loopback AND the LAN.
        _urls.append(f"http://localhost:{_port}")
        # Inside a container the detected IP is the container's, not the host's, and the
        # published-port mapping is unknown here — so don't print a misleading LAN URL.
        _lan_ip = None if _in_docker else _primary_lan_ip()
        if _lan_ip:
            _urls.append(f"http://{_lan_ip}:{_port}  (LAN — no built-in auth; trusted networks only)")
        elif _in_docker:
            _urls.append(f"(in Docker: reach it at http://<host>:<published-port>)")
    else:
        _urls.append(f"http://{_bind_host}:{_port}")
        if _bind_host == "127.0.0.1":
            _urls.append(f"http://localhost:{_port}")
    log.info(f"  Access:   {_urls[0]}")
    for _u in _urls[1:]:
        log.info(f"            {_u}")

    # ── Anthropic Claude API key ───────────────────────────────────────────
    state._env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    stored_claude = state._config.get("claude", {}).get("api_key", "")
    if state._env_key:
        state._config.setdefault("claude", {})["api_key"] = state._env_key
        masked = state._env_key[:8] + "…" + state._env_key[-4:] if len(state._env_key) > 12 else "***"
        log.info(f"  ANTHROPIC_API_KEY: env ({masked}) — takes precedence over config.json")
    elif stored_claude:
        log.info("  ANTHROPIC_API_KEY: from config.json")
    else:
        log.info("  ANTHROPIC_API_KEY: not set — Claude unavailable")

    # ── OpenAI API key ─────────────────────────────────────────────────────
    state._openai_env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    stored_openai = state._config.get("openai", {}).get("api_key", "")
    if state._openai_env_key:
        state._config.setdefault("openai", {})["api_key"] = state._openai_env_key
        masked = state._openai_env_key[:8] + "…" + state._openai_env_key[-4:] if len(state._openai_env_key) > 12 else "***"
        log.info(f"  OPENAI_API_KEY:    env ({masked}) — takes precedence over config.json")
    elif stored_openai:
        log.info("  OPENAI_API_KEY:    from config.json")
    else:
        log.info("  OPENAI_API_KEY:    not set — OpenAI unavailable")

    # ── Qwen / DashScope API key ───────────────────────────────────────────
    state._qwen_env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    stored_qwen = state._config.get("qwen", {}).get("api_key", "")
    if state._qwen_env_key:
        state._config.setdefault("qwen", {})["api_key"] = state._qwen_env_key
        masked = state._qwen_env_key[:8] + "…" + state._qwen_env_key[-4:] if len(state._qwen_env_key) > 12 else "***"
        log.info(f"  DASHSCOPE_API_KEY: env ({masked}) — takes precedence over config.json")
    elif stored_qwen:
        log.info("  DASHSCOPE_API_KEY: from config.json")
    else:
        log.info("  DASHSCOPE_API_KEY: not set — Qwen unavailable")

    # ── Mistral API key ────────────────────────────────────────────────────
    state._mistral_env_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    stored_mistral = state._config.get("mistral", {}).get("api_key", "")
    if state._mistral_env_key:
        state._config.setdefault("mistral", {})["api_key"] = state._mistral_env_key
        masked = state._mistral_env_key[:8] + "…" + state._mistral_env_key[-4:] if len(state._mistral_env_key) > 12 else "***"
        log.info(f"  MISTRAL_API_KEY:   env ({masked}) — takes precedence over config.json")
    elif stored_mistral:
        log.info("  MISTRAL_API_KEY:   from config.json")
    else:
        log.info("  MISTRAL_API_KEY:   not set — Mistral unavailable")

    # ── Groq API key ───────────────────────────────────────────────────────
    state._groq_env_key = os.environ.get("GROQ_API_KEY", "").strip()
    stored_groq = state._config.get("groq", {}).get("api_key", "")
    if state._groq_env_key:
        state._config.setdefault("groq", {})["api_key"] = state._groq_env_key
        masked = state._groq_env_key[:8] + "…" + state._groq_env_key[-4:] if len(state._groq_env_key) > 12 else "***"
        log.info(f"  GROQ_API_KEY:      env ({masked}) — takes precedence over config.json")
    elif stored_groq:
        log.info("  GROQ_API_KEY:      from config.json")
    else:
        log.info("  GROQ_API_KEY:      not set — Groq unavailable")

    # ── Google Gemini API key ──────────────────────────────────────────────
    state._gemini_env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    stored_gemini = state._config.get("gemini", {}).get("api_key", "")
    if state._gemini_env_key:
        state._config.setdefault("gemini", {})["api_key"] = state._gemini_env_key
        masked = state._gemini_env_key[:8] + "…" + state._gemini_env_key[-4:] if len(state._gemini_env_key) > 12 else "***"
        log.info(f"  GEMINI_API_KEY:    env ({masked}) — takes precedence over config.json")
    elif stored_gemini:
        log.info("  GEMINI_API_KEY:    from config.json")
    else:
        log.info("  GEMINI_API_KEY:    not set — Gemini unavailable")

    # Redis host/port env overrides — used by the Docker image to point at the
    # `redis` compose service without editing config.json.
    _redis_host_env = os.environ.get("REDIRECALL_REDIS_HOST", "").strip()
    _redis_port_env = os.environ.get("REDIRECALL_REDIS_PORT", "").strip()
    if _redis_host_env:
        state._config.setdefault("redis", {})["host"] = _redis_host_env
        log.info(f"  REDIS host:        env ({_redis_host_env})")
    if _redis_port_env:
        state._config.setdefault("redis", {})["port"] = int(_redis_port_env)
        log.info(f"  REDIS port:        env ({_redis_port_env})")

    rc = state._config.get("redis", {})
    log.info(f"  Ollama:   {state._config.get('ollama', {}).get('host')}:{state._config.get('ollama', {}).get('port')}")
    log.info(f"  Redis:    {rc.get('host')}:{rc.get('port')}  db={rc.get('db', 0)}")

    extra = state._config.get("redis_endpoints", [])
    if extra:
        log.info(f"  Extra Redis endpoints: {[e.get('name') for e in extra]}")
    log.info("=" * 60)

    # ── All slow work runs in background so startup returns immediately ────
    # SentenceTransformer model loading and Redis SCAN over all endpoints
    # can both take 5-60 s.  Doing them here would block Uvicorn from
    # accepting connections, causing a blank browser until they finish.
    async def _bg_init():
        # 1. Probe all endpoints — fast PING, marks unreachable ones offline
        await asyncio.to_thread(redis_store.refresh_endpoint_health)
        log.info(f"  Endpoint health: { {k: ('OK' if v else 'OFFLINE') for k, v in state._endpoint_health.items()} }")

        # 2. Warm up embedding model (5-15 s, CPU-bound)
        try:
            await asyncio.to_thread(embeddings.get_embed_model)
            log.info(f"  Embedding model ready: {state._embed_model_name}")
            embeddings._migrate_chunk_size_to_model_limit()
        except Exception as e:
            log.warning(f"  Embedding model warmup failed: {e}")

        # 2b. Warm the semantic cache and (if enabled) the cross-encoder reranker.
        # Both used to initialise lazily inside handle_chat, ON the event loop, so the
        # first question after a restart stalled seconds while the whole server froze.
        try:
            await asyncio.to_thread(cache._get_semantic_cache)
            log.info("  Semantic cache ready")
        except Exception as e:
            log.warning(f"  Semantic cache warmup failed: {e}")
        finally:
            # Warm done (success or not): from here cache_lookup/store use the cache
            # normally instead of skipping to avoid the inline build.
            state._semantic_cache_ready = True
        if state._config.get("reranker", {}).get("enabled", False):
            try:
                await asyncio.to_thread(embeddings.get_reranker)
                log.info("  Reranker ready")
            except Exception as e:
                log.warning(f"  Reranker warmup failed: {e}")

        # 3. Ensure FT indexes only on reachable endpoints
        try:
            instances = await asyncio.to_thread(rag_admin.list_rag_instances)
            for inst in instances:
                inst_name = inst.get("name", "")
                ep_name   = inst.get("redis_endpoint", "default")
                if inst_name and state._endpoint_health.get(ep_name, True):
                    try:
                        rc_inst = rag_admin.rc_for_instance(inst_name)
                        rag._index_ensured.discard(inst_name)
                        await asyncio.to_thread(rag.ensure_rag_index, inst_name, rc_inst)
                    except Exception as idx_err:
                        log.warning(f"  Could not ensure index for '{inst_name}': {idx_err}")
        except Exception:
            pass

    asyncio.create_task(_bg_init())

    # ── Start background recrawl scheduler ────────────────────────────────
    state._recrawl_task = asyncio.create_task(ws._recrawl_loop())
    log.info("  Recrawl scheduler: started")

