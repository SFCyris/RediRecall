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
               provider: str = "", model: str = "", usage: dict | None = None) -> dict:
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
    if usage and "prompt" in usage:
        meta["usage"] = usage   # provider-reported token counts (real, not estimated)
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    if latency:
        meta["latency"] = latency
    if chunks:
        meta["chunks"] = [{
            # The citation number this chunk was given in the prompt. Without it a
            # reopened conversation cannot line its [n] markers up with the inspector.
            "n":         c.get("n", i),
            "source":    c.get("source", ""),
            "score":     c.get("score", 0),
            "relevance": c.get("relevance", c.get("score", 0)),
            "lexical":   bool(c.get("lexical", False)),
            "instance":  c.get("instance", ""),
            "text":     (c.get("text", "") or "")[:_SESSION_CHUNK_TEXT_MAX],
        } for i, c in enumerate(chunks, 1)]
    return meta


def _approx_tokens(content) -> int:
    """Cheap token estimate (chars/4) for a stored message. Multimodal list
    content (a vision turn) is measured by its text parts only — image data URIs
    are huge but are never re-sent from history, so counting them would evict real
    conversation. Never loads a tokenizer (this runs on every turn)."""
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content if isinstance(p, dict)) // 4
    return len(str(content)) // 4


def history_window(msgs: list, max_tokens: int = 3000, hard_cap: int = 20) -> list:
    """The most recent messages that fit an approximate token budget, in
    chronological order. A flat message-count window let a few verbose turns
    (tables, chart JSON) re-bill an unbounded prefix on every subsequent turn;
    this bounds the resent history by size. Always keeps at least the most recent
    message; `hard_cap` bounds the count as a secondary backstop."""
    if max_tokens <= 0:
        return msgs[-hard_cap:] if hard_cap else list(msgs)
    out: list = []
    used = 0
    window = msgs[-hard_cap:] if hard_cap else msgs
    for m in reversed(window):
        t = _approx_tokens(m.get("content", ""))
        if out and used + t > max_tokens:
            break
        out.append(m)
        used += t
    out.reverse()
    # The budget can truncate on an odd boundary, leaving an assistant turn first.
    # Anthropic and Gemini reject a message list whose first non-system turn is the
    # assistant/model role ("first message must use the user role"), so drop any
    # leading assistant turns — the window then starts on a user turn or is empty
    # (messages become [system, user], which is valid). The old flat [-10:] window
    # never hit this because sessions always end on an assistant turn at an even index.
    while out and out[0].get("role") == "assistant":
        out.pop(0)
    return out


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



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TOKEN USAGE — cumulative tally
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Per-turn usage lives in each turn's meta (above). This is the all-time counter
# across every session, so the user can see what a provider/model has consumed
# overall. One Redis hash, fields "<provider>:<model>:in|out|cached|cache_write".

_USAGE_KEY = "usage:cumulative"


def record_usage(provider: str, model: str, usage: dict) -> None:
    """Add one turn's provider-reported counts to the all-time tally."""
    try:
        pfx = f"{provider}:{model}"
        pipe = redis_store.r().pipeline(transaction=False)
        pipe.hincrby(_USAGE_KEY, f"{pfx}:in",  int(usage.get("prompt", 0)))
        pipe.hincrby(_USAGE_KEY, f"{pfx}:out", int(usage.get("completion", 0)))
        if usage.get("cached"):
            pipe.hincrby(_USAGE_KEY, f"{pfx}:cached", int(usage["cached"]))
        if usage.get("cache_write"):   # Claude cache-creation tokens, billed ~1.25x input
            pipe.hincrby(_USAGE_KEY, f"{pfx}:cache_write", int(usage["cache_write"]))
        pipe.execute()
    except Exception as e:
        log.debug(f"usage tally skipped: {e}")


def usage_totals() -> dict:
    """The all-time tally as {"provider:model": {"in", "out", "cached", "cache_write"}}.

    The four counts are disjoint, as the providers report them: ``in`` is fresh input,
    ``cached`` prompt-cache reads, ``cache_write`` cache creation, ``out`` generated.
    ``cached``/``cache_write`` are absent for providers that do not report them.

    Split on the LAST colon: a model id carries colons of its own (``qwen2.5:7b``),
    so partitioning on the first would truncate it and merge two models into one row.
    """
    out: dict = {}
    try:
        for k, v in redis_store.r().hgetall(_USAGE_KEY).items():
            k = k.decode() if isinstance(k, bytes) else k
            pfx, _, kind = k.rpartition(":")
            out.setdefault(pfx, {})[kind] = int(v)
    except Exception:
        pass
    return out


def clear_usage() -> None:
    """Reset the all-time tally. Per-turn usage already stored in session
    history is untouched — this only zeroes the cumulative counter."""
    try:
        redis_store.r().delete(_USAGE_KEY)
    except Exception as e:
        log.debug(f"usage clear skipped: {e}")
