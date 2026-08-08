# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.config — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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
from . import constants, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Set when config.json existed but could not be parsed. Surfaced by /api/health
# so a corrupt config is visible instead of silently masquerading as a fresh install.


def load_config() -> dict:
    """Load config.json and deep-merge with defaults so new keys always exist.

    A corrupt file is NOT silently swallowed. Falling back to DEFAULT_CONFIG points
    ``redis.port`` at 6379 while a local install serves 6389/6390 — the app then
    starts "healthy" against an empty Redis and every RAG instance looks wiped.
    The bad file is preserved as ``config.json.corrupt-<ts>`` (so the keys in it are
    recoverable), the error is logged loudly, and ``_config_load_error`` is set so
    the health endpoint reports degraded rather than ok.
    """
    state._config_load_error = ""
    if constants.CONFIG_PATH.exists():
        try:
            with open(constants.CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError(f"expected a JSON object, got {type(cfg).__name__}")
            merged = {**constants.DEFAULT_CONFIG, **cfg}
            # Shallow-merge each nested dict so we pick up new sub-keys
            for k, v in constants.DEFAULT_CONFIG.items():
                if isinstance(v, dict):
                    merged[k] = {**v, **cfg.get(k, {})}
            return merged
        except OSError as e:
            # Unreadable is NOT corrupt. A permission error (e.g. the container UID
            # changed across an image update) must never rename the user's valid
            # config out of the way — renaming needs only directory write access,
            # so the quarantine below would succeed and destroy a good file.
            state._config_load_error = f"{type(e).__name__}: {e}"
            log.error("=" * 70)
            log.error(f"config.json could not be READ ({state._config_load_error}).")
            log.error("It was left untouched. Running on built-in DEFAULTS this session —")
            log.error("fix the file permissions and restart rather than re-entering settings,")
            log.error("otherwise the first save will overwrite your real configuration.")
            log.error("=" * 70)
        except Exception as e:
            state._config_load_error = f"{type(e).__name__}: {e}"
            quarantine = constants.CONFIG_PATH.with_name(
                f"{constants.CONFIG_PATH.name}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            try:
                constants.CONFIG_PATH.replace(quarantine)
                kept = f" — kept as {quarantine.name}"
            except Exception:
                kept = " — could NOT be preserved"
            log.error("=" * 70)
            log.error(f"config.json could not be parsed ({state._config_load_error}){kept}.")
            log.error("Starting from built-in DEFAULTS — API keys and your Redis endpoint")
            log.error(f"are NOT the ones you configured (defaults point at "
                      f"{constants.DEFAULT_CONFIG['redis']['host']}:{constants.DEFAULT_CONFIG['redis']['port']}).")
            log.error("Restore the quarantined file or re-enter settings before ingesting.")
            log.error("=" * 70)
    return dict(constants.DEFAULT_CONFIG)


def save_config(cfg: dict):
    """
    Persist config to disk.
    API keys that were loaded from environment variables are always stripped
    before writing so they never end up on disk.
    """
    to_save = copy.deepcopy(cfg)
    if state._env_key and to_save.get("claude", {}).get("api_key") == state._env_key:
        to_save.setdefault("claude", {})["api_key"] = ""
    if state._openai_env_key and to_save.get("openai", {}).get("api_key") == state._openai_env_key:
        to_save.setdefault("openai", {})["api_key"] = ""
    if state._qwen_env_key and to_save.get("qwen", {}).get("api_key") == state._qwen_env_key:
        to_save.setdefault("qwen", {})["api_key"] = ""
    if state._mistral_env_key and to_save.get("mistral", {}).get("api_key") == state._mistral_env_key:
        to_save.setdefault("mistral", {})["api_key"] = ""
    if state._groq_env_key and to_save.get("groq", {}).get("api_key") == state._groq_env_key:
        to_save.setdefault("groq", {})["api_key"] = ""
    if state._gemini_env_key and to_save.get("gemini", {}).get("api_key") == state._gemini_env_key:
        to_save.setdefault("gemini", {})["api_key"] = ""
    # Atomic write: serialise fully, fsync, then rename over the target. A plain
    # open(path,"w") truncates first, so a crash / full disk / OOM mid-write leaves
    # a half-written file that parses as garbage on the next boot (see load_config).
    # A FIXED temp name lets two processes sharing DATA_DIR interleave writes into
    # the same file, and os.replace then publishes the mixture — the exact
    # corruption the atomic write exists to prevent.
    _fd, _tmp_name = tempfile.mkstemp(dir=str(constants.CONFIG_PATH.parent),
                                      prefix=f".{constants.CONFIG_PATH.name}.", suffix=".tmp")
    os.close(_fd)
    tmp = Path(_tmp_name)
    try:
        with open(tmp, "w") as f:
            json.dump(to_save, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, constants.CONFIG_PATH)      # atomic on POSIX and Windows
        # A good file is on disk now, so /api/health must stop reporting "degraded".
        state._config_load_error = ""
    except Exception:
        try:
            tmp.unlink(missing_ok=True)   # never leave a stray temp behind
        except Exception:
            pass
        raise


# ── Provider SDK client reuse ────────────────────────────────────────────────
# A fresh SDK client per request means a fresh connection pool and a fresh TLS
# handshake on every turn (~30 ms per cloud provider, measured) — and a first-turn
# conversation pays it three times (answer + title + HyDE). Cache by credentials so
# a key change still creates a new client.
_provider_clients: dict = {}

def _cached_client(kind: str, api_key: str, base_url: str):
    """Return a pooled SDK client for (kind, key, base_url), creating it on first use."""
    ck = (kind, api_key, base_url)
    c = _provider_clients.get(ck)
    if c is None:
        c = (_anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
             if kind == "anthropic" else
             _AsyncOpenAI(api_key=api_key, base_url=base_url))
        _provider_clients[ck] = c
    return c

def invalidate_provider_clients():
    """Drop cached clients (called on config save, since keys may have changed)."""
    _provider_clients.clear()


def compose_system_prompt(client_system: str | None) -> str:
    """
    Build the effective system prompt for a chat turn.

    The global base_instruction (Settings -> Templates -> Base Instruction) is
    always prepended; a selected template's system prompt is added on top of it
    (templates are additive to the base). If both are empty we fall back to a
    plain default so the model still gets a system message.
    """
    base   = (state._config.get("base_instruction") or "").strip()
    client = (client_system or "").strip()
    parts  = [p for p in (base, client) if p]
    return "\n\n".join(parts) if parts else "You are a helpful assistant."


# ── Secret redaction ──────────────────────────────────────────────────────────
# Config sent to the browser must never carry provider keys or passwords. Each
# stored secret is swapped for a sentinel on the way out and restored from the
# stored value on the way back in, so the UI can round-trip settings without
# ever seeing a secret — and a blank field still clears a key (blank != sentinel).

_SECRET_SENTINEL = "__REDIRECALL_SECRET_KEPT__"
_PROVIDER_SECRET_KEYS = ("claude", "openai", "qwen", "mistral", "groq", "gemini")


def _redact_secrets(cfg: dict) -> dict:
    """Deep copy of cfg with every set secret replaced by _SECRET_SENTINEL."""
    red = copy.deepcopy(cfg)
    for p in _PROVIDER_SECRET_KEYS:
        if isinstance(red.get(p), dict) and red[p].get("api_key"):
            red[p]["api_key"] = _SECRET_SENTINEL
    if isinstance(red.get("redis"), dict) and red["redis"].get("password"):
        red["redis"]["password"] = _SECRET_SENTINEL
    if isinstance(red.get("security"), dict) and red["security"].get("password"):
        red["security"]["password"] = _SECRET_SENTINEL
    for ep in red.get("redis_endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("password"):
            ep["password"] = _SECRET_SENTINEL
    return red


def _unredact_secrets(new_cfg: dict, old_cfg: dict) -> None:
    """In place: swap any sentinel secret in new_cfg back to the stored value."""
    for p in _PROVIDER_SECRET_KEYS:
        if isinstance(new_cfg.get(p), dict) and new_cfg[p].get("api_key") == _SECRET_SENTINEL:
            new_cfg[p]["api_key"] = (old_cfg.get(p) or {}).get("api_key", "")
    if isinstance(new_cfg.get("redis"), dict) and new_cfg["redis"].get("password") == _SECRET_SENTINEL:
        new_cfg["redis"]["password"] = (old_cfg.get("redis") or {}).get("password", "")
    if isinstance(new_cfg.get("security"), dict) and new_cfg["security"].get("password") == _SECRET_SENTINEL:
        new_cfg["security"]["password"] = (old_cfg.get("security") or {}).get("password", "")
    old_eps = {e.get("name"): e for e in (old_cfg.get("redis_endpoints") or []) if isinstance(e, dict)}
    for ep in new_cfg.get("redis_endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("password") == _SECRET_SENTINEL:
            ep["password"] = (old_eps.get(ep.get("name")) or {}).get("password", "")


# Retention caps for the two append-only JSON stores. Both are held in memory
# and rewritten whole, so the in-memory list must be trimmed too — otherwise it
# grows without bound while only the tail is ever persisted.
_MAX_LOGS     = 500
_MAX_FEEDBACK = 2000
# Per-field cap on a feedback POST. The store is rewritten in full on every
# rating, so one oversized body permanently inflates the cost of every later one.
_MAX_FEEDBACK_FIELD = 8000

# Largest single upload accepted. api_ingest_files reads the whole body into
# memory before touching disk, so without a bound one file can exhaust RAM and
# then the volume. Override with REDIRECALL_MAX_UPLOAD_MB.
_MAX_UPLOAD_BYTES = int(os.environ.get("REDIRECALL_MAX_UPLOAD_MB", "100")) * 1024 * 1024


def load_logs():
    """Load persisted ingestion log from disk into memory."""
    if constants.LOGS_PATH.exists():
        try:
            with open(constants.LOGS_PATH) as f:
                state._ingestion_logs = json.load(f)[-_MAX_LOGS:]
        except Exception as e:
            log.warning(f"Could not read {constants.LOGS_PATH.name} ({e}) — starting with an empty log")


def load_feedback():
    """Load persisted feedback from disk into memory.

    Without this the module-level ``_feedback`` list starts empty on every boot
    and ``api_feedback`` — which rewrites the whole file — truncates the store to
    a single entry on the first rating after a restart.
    """
    if constants.FEEDBACK_PATH.exists():
        try:
            with open(constants.FEEDBACK_PATH) as f:
                state._feedback = json.load(f)[-_MAX_FEEDBACK:]
        except Exception as e:
            log.warning(f"Could not read {constants.FEEDBACK_PATH.name} ({e}) — starting with empty feedback")


def append_log(entry: dict):
    """Append an ingestion event and keep the last _MAX_LOGS entries."""
    state._ingestion_logs.append(entry)
    del state._ingestion_logs[:-_MAX_LOGS]          # trim in memory, not just on disk
    with open(constants.LOGS_PATH, "w") as f:
        json.dump(state._ingestion_logs, f)

