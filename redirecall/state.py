# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.state — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import pathlib
import os
from collections import OrderedDict
from typing import Any, Optional
import httpx
import redis

log = logging.getLogger(__name__)


_semantic_cache: "SemanticCache | None" = None
_semantic_cache_ready = False
_search_available: dict[str, bool | None] = {}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GLOBAL STATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_config: dict = {}

# Redis client pool: "default" key → primary client; other keys → named endpoints
_redis_clients: dict[str, redis.Redis] = {}

# Runtime reachability of each configured Redis endpoint.
# Keys are endpoint names ("default", "RAG", …); value is True/False.
# Unknown endpoints are treated as reachable (optimistic default).
_endpoint_health: dict[str, bool] = {}

_embed_model: Optional[Any] = None  # SentenceTransformer, lazily loaded
_embed_model_name: str = ""

# In-memory session store: session_id → list of {role, content} dicts
# Bounded: every conversation ever opened used to stay resident for the life of
# the process, so RSS grew on uptime alone. Sessions are persisted in Redis, so
# evicting the least-recently-touched one only costs a reload on next access.
_MAX_LIVE_SESSIONS = int(os.environ.get("REDIRECALL_MAX_LIVE_SESSIONS", "500"))
_sessions: "OrderedDict[str, list]" = OrderedDict()


def touch_session(sid: str) -> None:
    """Mark a session most-recently-used and evict the coldest beyond the cap."""
    if sid in _sessions:
        _sessions.move_to_end(sid)
    while len(_sessions) > _MAX_LIVE_SESSIONS:
        old, _ = _sessions.popitem(last=False)
        log.debug(f"evicted session {old} from memory (still in Redis)")


def reap_finished_crawls() -> int:
    """Drop completed crawl tasks. They were never removed, so every crawl left a
    dead asyncio.Task behind for the life of the process."""
    done = [u for u, t in _crawl_tasks.items() if t.done()]
    for u in done:
        _crawl_tasks.pop(u, None)
        _crawl_gates.pop(u, None)
    return len(done)

_feedback: list = []
_ingestion_logs: list = []
_crawl_tasks: dict[str, asyncio.Task] = {}
# Seed URL -> gate. The event is SET while running and cleared while paused, so a
# worker awaiting it blocks between pages. Pausing between pages (rather than
# killing the task) keeps the queue, the visited set and the URL skip-list intact,
# so resuming continues instead of restarting.
_crawl_gates: dict[str, asyncio.Event] = {}
# Active crawl state — survives browser refresh/reconnect.
# Key: url.  Value: {instance, pages_done, chunks, errors, blocked, start_ts, done}
_active_crawls: dict[str, dict] = {}

# Active file-ingest jobs, the disk-file twin of the crawl state above. A file ingest
# used to be invisible the moment its SSE stream was lost: no way to see one running,
# and no way to stop one — closing the tab left it indexing with nobody watching.
# Key: job id.  Value: {job, instance, total, index, ok, errors, files, done, cancelled}
_active_ingests: dict[str, dict] = {}
# Job id -> cancel flag, SET to ask the ingest loop to stop before the next file.
# Separate from the state dict above because that one is returned as JSON.
_ingest_cancels: dict[str, asyncio.Event] = {}
# How many finished jobs to keep so a reconnecting UI can still read the outcome.
_INGEST_HISTORY = 20


# A job registered this long ago whose generator never ran will never run: the response
# body was dropped before the first chunk. Generous enough that a slow client cannot be
# mistaken for one.
_INGEST_START_GRACE = 60.0


def reap_finished_ingests(now: float | None = None) -> int:
    """Drop finished jobs beyond the keep-window, and any job that never started.

    Finished jobs are kept deliberately — a browser that reconnects after the stream
    ended still needs to learn how it ended — but keeping every one of them for the
    life of the process is a leak, which is what the crawl equivalent above does.

    The never-started case is a different leak with a worse symptom. Registration happens
    before the streaming response is returned; everything that clears a job lives in the
    generator's ``finally``. A client that disconnects before pulling the first chunk
    therefore leaves a job that is registered, not done, and unreachable — and the UI
    attaches to the first not-done job it finds, so that phantom freezes the ingest panel
    for good. Reaping it also removes the uploads it left behind.
    """
    import time as _time
    now = _time.time() if now is None else now
    dropped = 0

    for j, st in list(_active_ingests.items()):
        if st.get("done") or st.get("started"):
            continue
        if now - float(st.get("registered_at", now)) < _INGEST_START_GRACE:
            continue
        for name in st.get("upload_paths", []):
            try:
                pathlib.Path(name).unlink(missing_ok=True)
            except OSError:
                pass
        _active_ingests.pop(j, None)
        _ingest_cancels.pop(j, None)
        dropped += 1

    finished = [j for j, st in _active_ingests.items() if st.get("done")]
    stale = finished[:-_INGEST_HISTORY] if len(finished) > _INGEST_HISTORY else []
    for j in stale:
        _active_ingests.pop(j, None)
        _ingest_cancels.pop(j, None)
        dropped += 1
    return dropped

# Per-instance RAG query statistics (in-memory, reset on restart).
# Keys: instance name.  Values: counters used to derive hit-rate and avg score.
# Structure: {name: {queries, hits, chunks_total, score_sum}}
_rag_stats: dict[str, dict] = {}
_reranker: Optional["CrossEncoder"] = None
_reranker_model_name: str = ""
_recrawl_task: Optional[asyncio.Task] = None
_watch_task: Optional[asyncio.Task] = None


def _record_rag_stats(
    instance: str,
    results: list[dict],
    raw_results: list[dict] | None = None,
) -> None:
    """Update per-instance RAG stats after every search_rag() call.

    ``results``     — chunks that passed the similarity threshold (used for hit counting).
    ``raw_results`` — all KNN results before threshold filtering; used to track the best
                      raw score so we can detect threshold misconfiguration (e.g. good
                      matches getting filtered out because the threshold is too strict).
    """
    s = _rag_stats.setdefault(instance, {
        "queries": 0, "hits": 0, "chunks_total": 0, "score_sum": 0.0,
        "raw_score_sum": 0.0,  # sum of best raw scores (pre-threshold) across all queries
    })
    s["queries"] += 1
    # Best raw cosine among the pre-threshold candidates. Use max(), not [0]:
    # results are ordered by fused RRF rank, so [0] can be a keyword-only hit
    # with cosine 0 even when a strong vector match is present further down.
    best_raw = max((c.get("score", 0.0) for c in raw_results), default=0.0) if raw_results else 0.0
    s["raw_score_sum"] += best_raw
    if results:
        s["hits"]         += 1
        s["chunks_total"] += len(results)
        s["score_sum"]    += results[0]["score"]   # top-1 cosine similarity (post-filter)
# Active streaming task per session — used to cancel mid-stream when the client
# sends {"type":"abort"}.  Keyed by session id.
_chat_tasks: dict[str, asyncio.Task] = {}

# API keys sourced from environment variables — never persisted to disk
_env_key: str = ""          # ANTHROPIC_API_KEY
_openai_env_key: str = ""   # OPENAI_API_KEY
_qwen_env_key: str = ""     # DASHSCOPE_API_KEY
_mistral_env_key: str = ""  # MISTRAL_API_KEY
_groq_env_key: str = ""     # GROQ_API_KEY
_gemini_env_key: str = ""   # GEMINI_API_KEY

_config_load_error: str = ""
_shared_http_client: httpx.AsyncClient | None = None
_ollama_models_cache: list[dict] = []
_ollama_models_ts: float = 0.0
_rag_meta_gen = 0
