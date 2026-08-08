# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.sessions — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import json
import time
from . import redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SESSION PERSISTENCE (Redis-backed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SESSION_PREFIX = "session:"


def _session_key(sid: str) -> str:
    return f"{_SESSION_PREFIX}{sid}"


def load_session(sid: str) -> list:
    """Load a session's message list from Redis. Returns [] if not found or persistence disabled."""
    if not state._config.get("sessions", {}).get("persist", True):
        return []
    try:
        raw = redis_store.r().get(_session_key(sid))
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return []


# Chunk text stored alongside a session turn is capped so a long conversation
# cannot balloon the session record; the inspector shows the excerpt.
_SESSION_CHUNK_TEXT_MAX = 1200


def _turn_meta(chunks: list | None = None, latency: dict | None = None,
               provider: str = "", model: str = "") -> dict:
    """Metadata persisted with a stored turn.

    Sessions used to hold only {role, content}, so everything that makes an answer
    inspectable — its retrieved chunks, timings and the model that produced it —
    was lost the moment the page reloaded: no citations, no RAG inspector, and
    ratings/regenerate could no longer identify the turn they belonged to.

    Only fields the UI can render are kept, and chunk text is truncated.
    NOTE: prompt construction reads `role`/`content` explicitly, so this extra key
    is never sent to a provider.
    """
    meta: dict = {"ts": int(time.time())}
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    if latency:
        meta["latency"] = latency
    if chunks:
        meta["chunks"] = [{
            "source":    c.get("source", ""),
            "score":     c.get("score", 0),
            "relevance": c.get("relevance", c.get("score", 0)),
            "lexical":   bool(c.get("lexical", False)),
            "instance":  c.get("instance", ""),
            "text":     (c.get("text", "") or "")[:_SESSION_CHUNK_TEXT_MAX],
        } for c in chunks]
    return meta


def save_session(sid: str, messages: list):
    """Persist a session to Redis with the configured TTL."""
    if not state._config.get("sessions", {}).get("persist", True):
        return
    try:
        ttl = int(state._config.get("sessions", {}).get("ttl", 86400))
        redis_store.r().setex(_session_key(sid), ttl, json.dumps(messages))
    except Exception as e:
        log.warning(f"Session save failed for {sid}: {e}")


def delete_session_from_redis(sid: str):
    """Remove a session from Redis."""
    try:
        redis_store.r().delete(_session_key(sid))
    except Exception:
        pass


def list_sessions_from_redis() -> list[dict]:
    """Return session summaries from Redis, excluding sessions already in _sessions."""
    result = []
    try:
        client = redis_store.r()
        # Collect keys first, then fetch all values in a single pipeline.
        keys = []
        sids = []
        for k in client.scan_iter(f"{_SESSION_PREFIX}*", count=200):
            sid = k.decode().removeprefix(_SESSION_PREFIX)
            if sid in state._sessions:
                continue   # already handled by the in-memory dict
            keys.append(k)
            sids.append(sid)

        if not keys:
            return result

        pipe = client.pipeline(transaction=False)
        for k in keys:
            pipe.get(k)
        values = pipe.execute()   # one round-trip for all GETs

        for sid, raw in zip(sids, values):
            if not raw:
                continue
            msgs = json.loads(raw)
            preview = ""
            for m in reversed(msgs):
                if m.get("role") == "assistant":
                    preview = m.get("content", "")[:60]
                    break
            result.append({"id": sid, "messages": len(msgs), "preview": preview})
    except Exception:
        pass
    return result

