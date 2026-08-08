# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.cache — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import hashlib
import json
import re
from typing import Any, Optional
from redisvl.query.filter import Tag
from redisvl.extensions.cache.llm import SemanticCache
from . import constants, rag_admin, redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SEMANTIC CACHE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_cache_vectorizer() -> Any:
    """
    Build an HFTextVectorizer that uses the same sentence-transformer model
    as the RAG embedder.  redisvl uses this to embed queries and responses
    before storing/looking up cache entries.

    The short model name (e.g. "all-MiniLM-L6-v2") is expanded to its full
    HuggingFace path so redisvl can resolve it correctly.
    """
    from redisvl.utils.vectorize import HFTextVectorizer  # noqa: PLC0415
    model = state._config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
    if "/" not in model:
        model = f"sentence-transformers/{model}"
    return HFTextVectorizer(model=model)


def _get_semantic_cache() -> "SemanticCache | None":
    """
    Lazily create and return the shared SemanticCache.

    Uses redisvl's SemanticCache which stores prompt→response pairs as
    vector embeddings and performs approximate-nearest-neighbour lookup on
    every incoming query.  A query is a cache hit when its cosine distance
    to a stored prompt is ≤ distance_threshold (i.e. similarity ≥ threshold).

    Returns None if the cache is disabled in config or if the Redis Search
    module is not available (logged once, never repeated).
    """
    if not state._config.get("cache", {}).get("enabled", True):
        return None
    if state._semantic_cache is not None:
        return state._semantic_cache
    try:
        similarity_threshold = state._config.get("cache", {}).get("similarity_threshold", 0.92)
        ttl  = state._config.get("cache", {}).get("ttl", 3600)
        # SemanticCache uses cosine DISTANCE not SIMILARITY — convert
        def _build(overwrite: bool):
            return SemanticCache(
                name=constants.CACHE_PREFIX.rstrip(":"),   # "semcache"
                vectorizer=_make_cache_vectorizer(),
                distance_threshold=round(1.0 - similarity_threshold, 4),
                ttl=ttl,
                redis_client=redis_store.r(),
                # Scope tag — see _cache_scope(). A cached answer is only valid for the
                # exact corpus/provider/model/prompt it was produced under, so the scope
                # is an indexed TAG we filter on rather than part of the embedded text.
                filterable_fields=[{"name": "scope", "type": "tag"}],
                overwrite=overwrite,
            )
        try:
            state._semantic_cache = _build(overwrite=False)
        except Exception as e:
            # An index built by an older version has no `scope` field, and redisvl
            # refuses to reuse a mismatched schema — which would leave the cache
            # permanently disabled. A cache is disposable, so rebuild it once;
            # entries are re-earned on the next few queries.
            if "does not match" not in str(e):
                raise
            log.info("Semantic cache schema changed (scope filter added) — rebuilding index; "
                     "previously cached answers are discarded.")
            state._semantic_cache = _build(overwrite=True)
        log.info("SemanticCache initialised (redisvl, scope-filtered)")
    except Exception as e:
        if "unknown command" in str(e).lower():
            log.warning(
                "Semantic cache disabled — Redis Search module not available. "
                "Use Redis Stack or Redis Enterprise with the Search module enabled."
            )
        else:
            log.warning(f"SemanticCache init failed: {e}")
        state._semantic_cache = None
    return state._semantic_cache


_VISUAL_INTENT_RE = re.compile(
    r"\b(charts?|graphs?|graphing|plots?|plotting|diagrams?|histograms?|scatter|"
    r"bar\s*(?:graph|chart)|pie\s*chart|line\s*(?:graph|chart)|"
    r"visuali[sz]e|visuali[sz]ations?)\b"
    r"|\by\s*=\s*\S"        # "y = <expr>" — a function to plot (e.g. "render function y=3x+sin(x)*x")
    r"|\bf\s*\(\s*x\s*\)",  # f(x)
    re.I,
)


def wants_visual(query: str) -> bool:
    """
    True when the query asks for a chart/graph/plot.

    The semantic cache keys only on query text, so without this a chart request
    that is similar to an earlier text answer would return that text (no chart),
    and two similar chart requests would return the first one's SVG. We skip the
    cache (lookup and store) for visual-intent queries so every chart is fresh.
    """
    return bool(_VISUAL_INTENT_RE.search(query or ""))


async def _effective_rag_instances(rag_instances: list[str] | None) -> list[str]:
    """The instances a query will actually search — disabled ones are dropped.

    The cache scope must be built from this, not from the requested list. An
    instance toggled off produces an ungrounded answer; scoping it as "answered
    against X" meant re-enabling X replayed that ungrounded answer as a hit.
    """
    out: list[str] = []
    for name in rag_instances or []:
        try:
            meta, _ep = await rag_admin._rag_meta_cached_async(name)
            if (meta or {}).get("enabled", True):
                out.append(name)
        except Exception:
            out.append(name)   # unknown state: assume it counts, never cross scopes
    return out


def _cache_scope(rag_instances: list[str] | None = None, provider: str = "",
                 model: str = "", source_filter: str = "",
                 system_prompt: str = "") -> str:
    """Identity of the *conditions* an answer was produced under.

    A semantic cache keyed on the question alone is wrong in two ways:

      * **Correctness** — the same question against a different knowledge base,
        provider, model or system prompt has a different correct answer, but the
        cache would replay the first one (together with the *other* corpus's
        chunks as its provenance).
      * **Privacy** — on a shared instance, an answer derived from one user's
        uploaded document would be served to anyone whose question landed within
        the similarity threshold.

    Everything that can change the answer therefore goes into a scope tag, and
    lookups are filtered to a matching scope. Instances are sorted so that
    ``[a,b]`` and ``[b,a]`` share a cache.
    """
    payload = "|".join([
        ",".join(sorted(rag_instances or [])),
        provider or "",
        model or "",
        source_filter or "",
        hashlib.sha256((system_prompt or "").encode()).hexdigest()[:16],
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def cache_lookup(query: str, threshold: float = 0.92, scope: str = "") -> dict | None:
    """
    Look up the nearest cached response via redisvl SemanticCache.
    Returns {"response": str, "score": float} or None.

    ``scope`` (see _cache_scope) restricts the search to entries produced under
    the same corpus/provider/model/prompt; an empty scope matches only entries
    stored without one.
    """
    # During the first few seconds after a restart the background warm may not have built
    # the vectorizer yet; treat that as a cache miss rather than paying the ~3 s build here.
    if state._semantic_cache is None and not state._semantic_cache_ready:
        return None
    cache = _get_semantic_cache()
    if cache is None:
        return None
    try:
        hits = cache.check(prompt=query, num_results=1,
                           filter_expression=Tag("scope") == (scope or "_none"))
        if hits:
            h = hits[0]
            dist  = float(h.get("vector_distance", h.get("score", 1.0)))
            score = round(1.0 - dist, 4)
            if score >= threshold:
                try:
                    meta = h.get("metadata") or {}
                    cached_chunks = json.loads(meta.get("chunks_json", "[]"))
                except Exception:
                    cached_chunks = []
                return {"response": h.get("response", ""), "score": score, "entry_id": h.get("entry_id", ""), "chunks": cached_chunks}
    except Exception as e:
        log.error(f"Cache lookup error: {e}")
    return None


def cache_store(query: str, response: str, chunks: list | None = None, scope: str = ""):
    """Store a query→response pair in the SemanticCache with TTL.
    Chunks are stored as JSON metadata so they can be re-displayed on cache hits.
    ``scope`` tags the entry with the conditions it was produced under so a later
    lookup under different conditions cannot match it (see _cache_scope).
    """
    # Skip storing during the warm window rather than triggering the ~3 s inline build.
    if state._semantic_cache is None and not state._semantic_cache_ready:
        return
    cache = _get_semantic_cache()
    if cache is None:
        return
    try:
        metadata = {"chunks_json": json.dumps(chunks)} if chunks else None
        cache.store(prompt=query, response=response, metadata=metadata,
                    filters={"scope": scope or "_none"})
    except Exception as e:
        log.error(f"Cache store error: {e}")

