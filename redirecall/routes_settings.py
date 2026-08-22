# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_settings — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import httpx
try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False
try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False
try:
    from openai import AsyncOpenAI as _AsyncOpenAI
    _OPENAI_SDK_AVAILABLE = True
except ImportError:
    _OPENAI_SDK_AVAILABLE = False
import redis
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
try:
    import fitz          # PyMuPDF — PDF text extraction
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
try:
    import trafilatura   # Best-in-class web content extraction
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
try:
    from bs4 import BeautifulSoup   # HTML parsing fallback + link extraction
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
try:
    from crawl4ai import AsyncWebCrawler as _C4AIWebCrawler   # parallel JS-capable crawler
    from crawl4ai import BrowserConfig  as _C4AIBrowserConfig
    from crawl4ai import CrawlerRunConfig as _C4AIRunConfig
    HAS_CRAWL4AI = True
except ImportError:
    HAS_CRAWL4AI = False
try:
    from sentence_transformers import CrossEncoder
    HAS_CROSSENCODER = True
except ImportError:
    HAS_CROSSENCODER = False
try:
    from docx import Document as _DocxDocument   # python-docx — DOCX text extraction
    HAS_PYTHON_DOCX = True
except ImportError:
    HAS_PYTHON_DOCX = False
try:
    from openpyxl import load_workbook as _load_workbook  # openpyxl — XLSX text extraction
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False
from . import appcore, config, constants, embeddings, providers, rag_admin, redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/config")
def api_get_config():
    """
    Return the full runtime config.
    Includes boolean flags indicating whether API keys came from env vars
    so the UI can show the correct hint without ever seeing the actual keys.
    """
    return {
        **config._redact_secrets(state._config),
        # Read-only: lets the UI offer "Reset to default" for the base instruction,
        # so an improved shipped default can be adopted after it has been saved once.
        "base_instruction_default": constants.DEFAULT_BASE_INSTRUCTION,
        "claude_key_from_env":  bool(state._env_key),
        "openai_key_from_env":  bool(state._openai_env_key),
        "qwen_key_from_env":    bool(state._qwen_env_key),
        "mistral_key_from_env": bool(state._mistral_env_key),
        "groq_key_from_env":    bool(state._groq_env_key),
        "gemini_key_from_env":  bool(state._gemini_env_key),
    }


@appcore.app.post("/api/config")
async def api_save_config(payload: dict):
    """
    Save updated config.
    After merging the payload, env-sourced API keys are always restored so a
    settings save never loses a key that was loaded from the environment.
    """
    # Restore any secrets the UI posted back as sentinels before applying,
    # so a settings save never wipes a stored key/password it never received.
    config._unredact_secrets(payload, state._config)

    # ── Embedding-model change guard ────────────────────────────────────────
    # An index is created with the dimension of whatever model was loaded at the
    # time. Switching models changes the query vector's dimension but NOT the
    # existing index, so every subsequent search raises a dim mismatch that the
    # generic handler swallows — RAG silently returns nothing, forever, with a
    # green health check. Refuse the change while indexed data exists unless the
    # caller explicitly forces it, and on force drop the schema markers so every
    # index is rebuilt at the new dimension.
    old_model = (state._config.get("embedding") or {}).get("model", "")
    new_model = (payload.get("embedding") or {}).get("model", old_model)
    if new_model and new_model != old_model:
        populated = []
        try:
            for inst in await asyncio.to_thread(rag_admin.list_rag_instances):
                if inst.get("chunks", 0) > 0:
                    populated.append(f"{inst.get('name')} ({inst['chunks']} chunks)")
        except Exception:
            pass
        if populated and not payload.get("force_embedding_change"):
            raise HTTPException(409, detail={
                "error": "embedding_model_change_requires_reindex",
                "message": (
                    f"Switching the embedding model from '{old_model}' to '{new_model}' "
                    f"invalidates every existing index — the stored vectors have the old "
                    f"dimension and would stop matching, making RAG return nothing. "
                    f"Re-ingest is required afterwards."
                ),
                "instances": populated,
                "hint": "Re-send with force_embedding_change=true to proceed and rebuild the indexes.",
            })
        # Reset the schema markers on EVERY model change, not just when instances
        # currently hold indexed documents. After a previous mismatched switch the
        # indexed count is 0 — precisely the state a user switching back is trying
        # to repair — and gating on `populated` there would skip the rebuild and
        # leave the index stuck at the wrong dimension.
        log.warning(f"Embedding model {old_model!r} -> {new_model!r}: resetting index "
                    f"schema markers so every index is rebuilt at the new dimension"
                    + (f" (affects: {', '.join(populated)})" if populated else ""))
        await redis_store._reset_index_markers_for_embedding_change()

    state._config.update(payload)

    # Restore env-sourced keys — they must never be overwritten by a config save
    if state._env_key:
        state._config.setdefault("claude", {})["api_key"] = state._env_key
    if state._openai_env_key:
        state._config.setdefault("openai", {})["api_key"] = state._openai_env_key
    if state._qwen_env_key:
        state._config.setdefault("qwen", {})["api_key"] = state._qwen_env_key
    if state._mistral_env_key:
        state._config.setdefault("mistral", {})["api_key"] = state._mistral_env_key
    if state._groq_env_key:
        state._config.setdefault("groq", {})["api_key"] = state._groq_env_key
    if state._gemini_env_key:
        state._config.setdefault("gemini", {})["api_key"] = state._gemini_env_key

    config.save_config(state._config)      # strips env keys before writing to disk
    redis_store.invalidate_redis_clients()
    config.invalidate_provider_clients()   # keys/base URLs may have changed
    state._embed_model = None       # force model reload on next request

    log.info(
        f"Config saved — provider={state._config.get('provider')} "
        f"claude={'env' if state._env_key else ('set' if state._config.get('claude',{}).get('api_key') else 'empty')} "
        f"openai={'env' if state._openai_env_key else ('set' if state._config.get('openai',{}).get('api_key') else 'empty')}"
    )

    # Warm up connections in background — don't block the event loop
    async def _rewarm():
        try:
            await asyncio.to_thread(redis_store.get_redis)
            await asyncio.to_thread(embeddings.get_embed_model)
        except Exception:
            pass
    asyncio.create_task(_rewarm())

    return {"ok": True}


@appcore.app.get("/api/config/export")
def api_export_config():
    """Export current settings as a JSON download, with all secrets redacted.

    Secrets are replaced by a sentinel rather than the raw file so an export can
    be shared/backed up without leaking keys; re-importing keeps existing keys.
    """
    return JSONResponse(
        content=config._redact_secrets(state._config),
        headers={"Content-Disposition": 'attachment; filename="redirecall_config.json"'},
    )


@appcore.app.post("/api/config/import")
async def api_import_config(file: UploadFile = File(...)):
    content = await file.read()
    cfg = json.loads(content)
    # Sentinel secrets in an exported file mean "keep what's already stored".
    config._unredact_secrets(cfg, state._config)
    merged = {**constants.DEFAULT_CONFIG, **cfg}
    # Deep-merge like load_config does. A shallow merge of an uploaded file
    # containing e.g. {"redis": {"host": "nas"}} drops the port, silently sending
    # every query to 6379 and making all RAG instances look wiped.
    for k, v in constants.DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            merged[k] = {**v, **(cfg.get(k) or {})}

    # Importing a config that changes the embedding model has the same consequence
    # as changing it in Settings: every existing vector is the wrong dimension and
    # retrieval dies silently. Run the same guard rather than bypassing it.
    old_model = (state._config.get("embedding") or {}).get("model")
    new_model = (merged.get("embedding") or {}).get("model")
    if new_model and new_model != old_model:
        log.warning(f"Config import changes the embedding model {old_model!r} -> "
                    f"{new_model!r}: resetting index schema markers.")
        await redis_store._reset_index_markers_for_embedding_change()

    state._config = merged
    config.save_config(state._config)
    # Endpoints, provider keys and the embedding model may all have changed.
    redis_store.invalidate_redis_clients()
    config.invalidate_provider_clients()
    return {"ok": True}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — STATUS / HEALTH CHECKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/health")
def api_health():
    """Lightweight liveness probe (no I/O) for the Docker HEALTHCHECK and run.sh.

    Always returns 200 while the process is up. The ``services`` booleans report
    which optional capabilities were importable at startup so run.sh can show
    them in its status banner. Redis reachability lives on /api/status/redis
    (kept out of here so this probe never blocks on a slow connection).
    """
    return {
        # "degraded" when config.json failed to parse: the process is alive, but it
        # is running on built-in defaults (a different Redis), which otherwise looks
        # identical to a healthy empty install.
        "status":   "degraded" if state._config_load_error else "ok",
        **({"config_error": state._config_load_error} if state._config_load_error else {}),
        "app":      "RediRecall",
        "version":  constants.__version__,
        # AGPL-3.0 §13: users interacting with this instance over a network must be
        # offered the Corresponding Source. The UI links this too; keeping it here
        # makes the offer available to any client, not just the browser UI.
        "license":  "AGPL-3.0-or-later",
        "source":   constants.SOURCE_URL,
        "provider": state._config.get("provider", ""),
        "services": {
            "pdf":         HAS_PYMUPDF,
            "docx":        HAS_PYTHON_DOCX,
            "xlsx":        HAS_OPENPYXL,
            "web_extract": HAS_TRAFILATURA,
            "html_parse":  HAS_BS4,
            "reranker":    HAS_CROSSENCODER,
            "js_crawl":    HAS_CRAWL4AI,
            "anthropic":   _ANTHROPIC_AVAILABLE,
            "openai_sdk":  _OPENAI_SDK_AVAILABLE,
            "gemini_sdk":  _GENAI_AVAILABLE,
        },
    }


def probe_search(rc: redis.Redis, ep_name: str = "default") -> bool:
    """
    Check whether a Redis connection has the Search module (RediSearch) loaded.

    The result is cached in ``_search_available`` so we only probe once per
    endpoint per server lifetime — subsequent calls return instantly.

    Returns True if FT commands are available, False otherwise.
    """
    cached = state._search_available.get(ep_name)
    if cached is not None:
        return cached
    try:
        # FT.INFO on a non-existent index raises "Unknown index name" (not
        # "unknown command") when Search IS available — so any response other
        # than an "unknown command" error means Search is present.
        rc.execute_command("FT.INFO", "__probe__")
        state._search_available[ep_name] = True
    except Exception as e:
        err = str(e).lower()
        if "unknown command" in err or "unknown subcommand" in err:
            state._search_available[ep_name] = False
        else:
            # "Unknown index name" or similar — Search is available, index just doesn't exist
            state._search_available[ep_name] = True
    return state._search_available[ep_name]


@appcore.app.get("/api/status/redis")
def api_redis_status():
    """Ping the primary Redis, refresh endpoint health, and return server info."""
    # Refresh health for all endpoints on every status poll (called every 30 s by the UI)
    redis_store.refresh_endpoint_health()
    try:
        rc = redis_store.get_redis()
        info = rc.info()
        return {
            "ok": True,
            "version":           info.get("redis_version"),
            "memory_used":       info.get("used_memory_human"),
            "memory_peak":       info.get("used_memory_peak_human"),
            "connected_clients": info.get("connected_clients"),
            "uptime_days":       info.get("uptime_in_days"),
            "mode":              info.get("redis_mode", "standalone"),
            "search_available":  probe_search(rc, "default"),
            "endpoint_health":   dict(state._endpoint_health),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.get("/api/status/redis/{endpoint_name}")
def api_redis_endpoint_status(endpoint_name: str):
    """Ping a named Redis endpoint and return Search module availability."""
    try:
        rc = redis_store.r_for(endpoint_name)
        info = rc.info()
        return {
            "ok":               True,
            "version":          info.get("redis_version"),
            "memory_used":      info.get("used_memory_human"),
            "search_available": probe_search(rc, endpoint_name),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.get("/api/status/ollama")
async def api_ollama_status():
    """Ping the Ollama server (reuses the cached model list to avoid a redundant /api/tags call)."""
    try:
        await providers.ollama_models()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _probe_key(request: Request) -> str | None:
    """A one-off API key to test, taken from the POST body.

    It used to arrive as ``?key=…``. A query string is written to the server's access
    log, to every proxy log in front of it, and to the browser's own history — so the
    one endpoint whose whole purpose is handling a credential was the one leaking it.
    A request body is in none of those. GET keeps working as the no-key health check
    against the already-configured credential.

    The Settings form pre-fills a saved key's field with _SECRET_SENTINEL (the redacted
    placeholder from GET /api/config) and the Test button sends whatever is in the field,
    so the sentinel must fall through to the stored key rather than be tried as one.
    """
    if request.method != "POST":
        return None
    try:
        payload = await request.json()
    except Exception:
        return None
    key = payload.get("key") if isinstance(payload, dict) else None
    return None if (not key or key == config._SECRET_SENTINEL) else str(key)


@appcore.app.api_route("/api/status/claude", methods=["GET", "POST"])
async def api_claude_status(request: Request):
    """Verify the Claude API key. POST {"key": "..."} to test an unsaved one."""
    key = await _probe_key(request)
    api_key  = key or state._config.get("claude", {}).get("api_key", "")
    base_url = state._config.get("claude", {}).get("base_url", "https://api.anthropic.com").rstrip("/")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"{base_url}/v1/messages/count_tokens",
                json={"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            )
            if res.status_code == 200:
                return {"ok": True}
            if res.status_code == 401:
                return {"ok": False, "error": res.json().get("error", {}).get("message", "Unauthorized")}
            # Fallback
            res2 = await client.get(
                f"{base_url}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            if res2.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": res2.json().get("error", {}).get("message", f"HTTP {res2.status_code}")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.api_route("/api/status/openai", methods=["GET", "POST"])
async def api_openai_status(request: Request):
    """Verify the OpenAI API key using the native SDK. POST {"key": "..."} to test an unsaved one."""
    key = await _probe_key(request)
    api_key  = key or state._config.get("openai", {}).get("api_key", "")
    base_url = state._config.get("openai", {}).get("base_url", "https://api.openai.com").rstrip("/")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured"}
    if not _OPENAI_SDK_AVAILABLE:
        return {"ok": False, "error": "openai package not installed. Run: pip install openai"}
    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        await client.models.list()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.api_route("/api/status/qwen", methods=["GET", "POST"])
async def api_qwen_status(request: Request):
    """Verify the Qwen API key. POST {"key": "..."} to test an unsaved one."""
    key = await _probe_key(request)
    api_key  = key or state._config.get("qwen", {}).get("api_key", "")
    base_url = state._config.get("qwen", {}).get("base_url",
                           "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured. Get a free key at qwen.ai"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if res.status_code == 200:
                return {"ok": True}
            body = res.json()
            return {"ok": False, "error": body.get("message", f"HTTP {res.status_code}")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.api_route("/api/status/mistral", methods=["GET", "POST"])
async def api_mistral_status(request: Request, probe: bool = False):
    """Verify the Mistral API key. POST {"key": "..."} to test an unsaved one.
    The cheap check is GET /models; some free-tier keys are denied that specific
    endpoint (401) while chat completions work fine, so a bare /models 401 is not
    proof the key itself is invalid. The periodic background poll (checkCloudStatus,
    every 5 min) relies on the cheap check alone and accepts that rare imprecision.
    ?probe=1 — sent only by the user-initiated Settings "Test" button — confirms a
    /models failure with one minimal real chat-completion call before reporting the
    key as invalid."""
    key = await _probe_key(request)
    api_key  = key or state._config.get("mistral", {}).get("api_key", "")
    base_url = state._config.get("mistral", {}).get("base_url", "https://api.mistral.ai/v1").rstrip("/")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured. Get a free key at console.mistral.ai"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if res.status_code == 200:
                return {"ok": True}
            try:
                body = res.json()
                msg = body.get("message") or body.get("error") or f"HTTP {res.status_code}"
            except Exception:
                msg = f"HTTP {res.status_code}"
            if not probe or not _OPENAI_SDK_AVAILABLE:
                return {"ok": False, "error": msg}
            try:
                model = state._config.get("mistral", {}).get("model") or providers.MISTRAL_MODELS_STATIC[0]["id"]
                client2 = config._cached_client("openai", api_key, base_url)
                await client2.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=1,
                )
                return {"ok": True}
            except Exception as e2:
                return {"ok": False, "error": f"Mistral error: {e2}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.api_route("/api/status/groq", methods=["GET", "POST"])
async def api_groq_status(request: Request):
    """Verify the Groq API key using the openai SDK. POST {"key": "..."} to test an unsaved one."""
    key = await _probe_key(request)
    api_key  = key or state._config.get("groq", {}).get("api_key", "")
    base_url = state._config.get("groq", {}).get("base_url", "https://api.groq.com/openai").rstrip("/")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured. Get a free key at console.groq.com"}
    if not _OPENAI_SDK_AVAILABLE:
        return {"ok": False, "error": "openai package not installed. Run: pip install openai"}
    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        await client.models.list()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.api_route("/api/status/gemini", methods=["GET", "POST"])
async def api_gemini_status(request: Request, probe: bool = False):
    """Verify the Gemini API key using the native SDK. POST {"key": "..."} to test an
    unsaved one. The cheap check is models.list(); some keys are denied that
    specific method (403 PERMISSION_DENIED) while generateContent still works
    fine, so a bare list() failure is not proof the key itself is invalid. The
    periodic background poll (checkCloudStatus, every 5 min) relies on the cheap
    check alone and accepts that rare imprecision. ?probe=1 — sent only by the
    user-initiated Settings "Test" button — confirms a list() failure with one
    minimal real generateContent call before reporting the key as invalid."""
    key = await _probe_key(request)
    api_key = key or state._config.get("gemini", {}).get("api_key", "")
    if not api_key:
        return {"ok": False, "configured": False, "error": "No API key configured. Get a free key at aistudio.google.com"}
    if not _GENAI_AVAILABLE:
        return {"ok": False, "error": "google-genai not installed. Run: pip install google-genai"}
    client = _google_genai.Client(api_key=api_key)
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: next(iter(client.models.list()), None))
        return {"ok": True}
    except Exception as e:
        if not probe:
            return {"ok": False, "error": providers._gemini_err_msg(e)}
        try:
            model = state._config.get("gemini", {}).get("model") or providers.GEMINI_MODELS_STATIC[0]["id"]
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.models.generate_content(model=model, contents="hi"))
            return {"ok": True}
        except Exception as e2:
            return {"ok": False, "error": providers._gemini_err_msg(e2)}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/ollama/models")
async def api_ollama_models():
    return await providers.ollama_models()


@appcore.app.post("/api/ollama/pull")
async def api_ollama_pull(payload: dict):
    """Pull a model onto the local Ollama server, proxying its progress stream.

    NDJSON lines pass through as-is ({"status", "total", "completed", ...});
    Content-Encoding: identity bypasses the gzip middleware, which would buffer
    the stream and turn live progress into one burst at the end."""
    name = str(payload.get("model", "")).strip()
    if not name:
        raise HTTPException(400, "model is required")

    async def gen():
        # finally, not trailing code: a client that disconnects mid-pull closes
        # this generator with GeneratorExit, which `except Exception` never sees
        # — the invalidation must survive that path too (the pull continues
        # server-side inside Ollama either way).
        try:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", f"{providers.ollama_base()}/api/pull",
                                             json={"model": name, "stream": True}) as resp:
                        async for line in resp.aiter_lines():
                            if line.strip():
                                yield line + "\n"
            except Exception as e:
                yield json.dumps({"error": str(e)}) + "\n"
        finally:
            state._ollama_models_ts = 0.0

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Encoding": "identity"})


@appcore.app.delete("/api/ollama/models/{name:path}")
async def api_ollama_delete(name: str):
    """Remove a model from the local Ollama server ({name:path} — model names
    contain '/' and ':'). Sends both key spellings; Ollama accepted "name"
    historically and "model" on newer versions."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.request("DELETE", f"{providers.ollama_base()}/api/delete",
                                       json={"model": name, "name": name})
        state._ollama_models_ts = 0.0   # invalidate the cached list
        if res.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"Ollama returned {res.status_code}: {res.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@appcore.app.get("/api/claude/models")
def api_claude_models():
    return providers.stamp_vision(providers.CLAUDE_MODELS)

@appcore.app.get("/api/openai/models")
async def api_openai_models():
    return providers.stamp_vision(await providers.openai_models())

@appcore.app.get("/api/qwen/models")
def api_qwen_models():
    return providers.stamp_vision(providers.QWEN_MODELS_STATIC)

@appcore.app.get("/api/mistral/models")
async def api_mistral_models():
    """Fetch live models from Mistral's /v1/models; fall back to the static list."""
    api_key  = state._config.get("mistral", {}).get("api_key", "")
    base_url = state._config.get("mistral", {}).get("base_url", "https://api.mistral.ai/v1").rstrip("/")
    if not api_key:
        return providers.stamp_vision(providers.MISTRAL_MODELS_STATIC)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"{base_url}/models",
                                   headers={"Authorization": f"Bearer {api_key}"})
            if res.status_code == 200:
                models = providers.filter_mistral_models(res.json().get("data", []))
                if models:
                    return models
    except Exception:
        pass
    return providers.stamp_vision(providers.MISTRAL_MODELS_STATIC)

@appcore.app.get("/api/groq/models")
async def api_groq_models():
    return providers.stamp_vision(await providers.groq_models())

@appcore.app.get("/api/gemini/models")
async def api_gemini_models():
    # The live path stamps vision itself; this covers the static fallback taken when
    # there is no key or the fetch fails, whose entries carry no flag at all.
    return providers.stamp_vision(await providers.gemini_models())

