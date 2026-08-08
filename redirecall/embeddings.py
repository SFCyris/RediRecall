# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.embeddings — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
from typing import Any, Optional
import numpy as np
try:
    from sentence_transformers import CrossEncoder
    HAS_CROSSENCODER = True
except ImportError:
    HAS_CROSSENCODER = False
from . import config, state, textutil

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EMBEDDING HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EMBEDDING MODEL REGISTRY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Every chunk records which model produced its vector, as the small integer id
# below. Ids are a permanent contract: never renumber or reuse one, because
# stored chunks reference them. Append new models with the next free id.
#
# The id is what makes a mixed-model instance possible later: a corpus can hold
# vectors from several models, and a query is then embedded once per model
# present and the result sets fused. Vectors of different widths cannot share
# one RediSearch vector field, so such an index needs one field per dimension —
# hence `field` below, which is already dimension-derived rather than fixed.
#
# E5 models require an asymmetric prefix: "query: " on the search text and
# "passage: " on the stored text. Omitting it does not error, it just quietly
# degrades retrieval, so the prefixes live here rather than at the call sites.
EMBEDDING_MODELS: dict[int, dict] = {
    0: {
        "repo": "all-MiniLM-L6-v2", "dims": 384, "seq": 256,
        "label": "all-MiniLM-L6-v2 (legacy, English only)",
        "query_prefix": "", "passage_prefix": "", "multilingual": False,
    },
    1: {
        "repo": "intfloat/multilingual-e5-small", "dims": 384, "seq": 512,
        "label": "multilingual-e5-small (default)",
        "query_prefix": "query: ", "passage_prefix": "passage: ", "multilingual": True,
    },
    2: {
        "repo": "intfloat/multilingual-e5-base", "dims": 768, "seq": 512,
        "label": "multilingual-e5-base (higher quality, 2x vectors)",
        "query_prefix": "query: ", "passage_prefix": "passage: ", "multilingual": True,
    },
    3: {
        "repo": "BAAI/bge-m3", "dims": 1024, "seq": 8192,
        "label": "bge-m3 (long context, heaviest)",
        "query_prefix": "", "passage_prefix": "", "multilingual": True,
    },
}
DEFAULT_EMBEDDING_ID = 1
_REPO_TO_ID = {spec["repo"]: i for i, spec in EMBEDDING_MODELS.items()}


def embedding_id_for(repo: str | None = None) -> int:
    """Registry id for a model repo, or -1 when it is not a known model.

    -1 means "unknown provenance": the chunk is still stored and searchable, it
    just cannot take part in a mixed-model query.
    """
    repo = repo or (state._config.get("embedding", {}) or {}).get("model", "")
    return _REPO_TO_ID.get(repo, -1)


def embedding_spec(repo: str | None = None) -> dict:
    """Registry entry for a model repo, falling back to a neutral description."""
    mid = embedding_id_for(repo)
    if mid in EMBEDDING_MODELS:
        return EMBEDDING_MODELS[mid]
    return {"repo": repo or "", "dims": 0, "seq": 256, "label": repo or "custom",
            "query_prefix": "", "passage_prefix": "", "multilingual": False}


def vector_field_for(repo: str | None = None) -> str:
    """Vector field name for a model.

    Named by width, not by model: two models of the same dimension can share a
    field, and a mixed-model index gets one field per distinct width instead of
    one per model.
    """
    dims = embedding_spec(repo).get("dims") or 0
    return f"embedding_{dims}" if dims else "embedding"


def get_embed_model(name: str | None = None) -> Any:
    """
    Lazy-load the SentenceTransformer model.
    The sentence_transformers import is deferred here so Python startup is
    not blocked by the 5-15 s PyTorch initialisation time.
    Reloads if the model name has changed (e.g. user switched in settings).
    """
    name = name or state._config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
    if state._embed_model is None or state._embed_model_name != name:
        log.info(f"Loading embedding model: {name}")
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        state._embed_model = SentenceTransformer(name)
        state._embed_model_name = name
        _warn_if_chunk_size_exceeds_model(state._embed_model, name)
    return state._embed_model


def _migrate_chunk_size_to_model_limit() -> None:
    """Clamp a saved ``rag.chunk_size`` that the embedding model cannot encode.

    Changing DEFAULT_CONFIG only helps new installs — an existing config.json keeps
    whatever was saved, so a value like 1024 words silently keeps discarding ~80% of
    every chunk before embedding. That is a defect rather than a preference, so it is
    corrected once, persisted, and logged with the reasoning. Values already within
    the model's limit are never touched.
    """
    try:
        model = get_embed_model()
        max_tokens = int(getattr(model, "max_seq_length", 0) or 0)
        if max_tokens <= 0:
            return
        safe = max(32, int((max_tokens - 2) / 1.3))
        rag = state._config.setdefault("rag", {})
        current = int(rag.get("chunk_size", 180))
        if current <= safe:
            return
        rag["chunk_size"] = safe
        # Keep the overlap proportionate rather than leaving it larger than the chunk.
        if int(rag.get("chunk_overlap", 32)) >= safe:
            rag["chunk_overlap"] = max(8, safe // 6)
        config.save_config(state._config)
        log.warning(
            f"  chunk_size {current} -> {safe}: {state._embed_model_name} encodes at most "
            f"{max_tokens} tokens, so {current} words was discarding roughly "
            f"{100 - int(max_tokens / (current * 1.3 + 2) * 100)}% of every chunk before "
            f"embedding. Existing chunks keep their old size until re-ingested."
        )
    except Exception as e:
        log.debug(f"chunk_size migration skipped: {e}")


def _rerank_candidate_k(top_k: int) -> int:
    """How many chunks to retrieve before reranking.

    The cross-encoder can only improve on the vector/BM25 ordering if it is given
    more candidates than the caller intends to keep — with candidates == top_k it
    permutes the same set and the feature is inert. Returns top_k unchanged when
    reranking is off, so a plain search does no extra work.
    """
    rr = state._config.get("reranker", {})
    if not rr.get("enabled", False):
        return top_k
    return max(top_k, int(state._config.get("rag", {}).get("rerank_candidates", 40)))


_chunk_size_warned: set[str] = set()


def _warn_if_chunk_size_exceeds_model(model: Any, name: str) -> None:
    """Warn once per model when chunk_size (words) exceeds what it can encode.

    SentenceTransformer truncates silently at ``max_seq_length`` tokens, so an
    oversized chunk is stored and shown in full while only its first part is
    represented by the vector — recall drops with no visible symptom.
    """
    if name in _chunk_size_warned:
        return
    _chunk_size_warned.add(name)
    try:
        max_tokens = int(getattr(model, "max_seq_length", 0) or 0)
        if max_tokens <= 0:
            return
        words = int(state._config.get("rag", {}).get("chunk_size", 180))
        # Measured on representative English prose rather than assumed: the real
        # ratio varies hugely by content type (see count_tokens), so this warning
        # is about the configured size for ordinary text. chunk_text applies a
        # hard per-chunk token guard for everything else.
        est_tokens = textutil.count_tokens(" ".join(["word"] * words))
        if est_tokens > max_tokens:
            safe = max(32, int((max_tokens - 2) / 1.3))
            log.warning(
                f"chunk_size={words} words ≈ {est_tokens} tokens exceeds {name}'s "
                f"{max_tokens}-token limit — roughly "
                f"{100 - int(max_tokens / est_tokens * 100)}% of each chunk is dropped "
                f"before embedding. Lower Settings → RAG → chunk size to ≤ {safe}."
            )
    except Exception:
        pass


def embed(text: str, is_query: bool = False) -> np.ndarray:
    """Embed a single string. Returns a normalised float32 vector.

    ``is_query`` selects the asymmetric prefix E5 models require. Defaulting to
    False means stored text is treated as a passage, which is the common case.
    """
    spec = embedding_spec()
    prefix = spec["query_prefix"] if is_query else spec["passage_prefix"]
    return get_embed_model().encode(prefix + text, normalize_embeddings=True)


def embed_batch(texts: list[str]) -> np.ndarray:
    """
    Embed a list of strings in one model call.
    Much faster than calling embed() in a loop because the model can use
    batched GPU/CPU operations.  Returns shape (N, dim) float32 array.
    """
    prefix = embedding_spec()["passage_prefix"]
    return get_embed_model().encode(
        [prefix + t for t in texts] if prefix else texts,
        normalize_embeddings=True,
        batch_size=32,          # tune based on available RAM/VRAM
        show_progress_bar=False,
    )


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two pre-normalised vectors (just a dot product)."""
    return float(np.dot(a, b))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CROSS-ENCODER RERANKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_reranker() -> "CrossEncoder | None":
    """Lazy-load the cross-encoder reranker model if enabled in config."""
    if not state._config.get("reranker", {}).get("enabled", False):
        return None
    if not HAS_CROSSENCODER:
        log.warning("cross-encoder reranking requested but sentence-transformers CrossEncoder not available")
        return None
    model = state._config.get("reranker", {}).get("model", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    if state._reranker is None or state._reranker_model_name != model:
        log.info(f"Loading cross-encoder reranker: {model}")
        try:
            state._reranker = CrossEncoder(model)
            state._reranker_model_name = model
        except Exception as e:
            log.warning(f"Cross-encoder load failed: {e}")
            return None
    return state._reranker


def rerank_chunks(query: str, chunks: list[dict], top_n: int = 5) -> list[dict]:
    """
    Re-rank retrieved chunks using a cross-encoder model.

    Cross-encoders jointly encode (query, passage) pairs and produce a
    relevance score that is much more accurate than bi-encoder cosine
    similarity.  This step runs after the fast HNSW+BM25 retrieval and
    re-orders the candidates so the most relevant chunks bubble to the top.

    Falls back to the original order if the reranker is unavailable.
    """
    reranker = get_reranker()
    if not reranker or not chunks:
        return chunks
    try:
        pairs  = [(query, c["text"]) for c in chunks]
        scores = reranker.predict(pairs)
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        result = [c for _, c in ranked[:top_n]]
        # Annotate each chunk with its reranker score for debugging/analytics
        for (score, _), chunk in zip(sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True), result):
            chunk["reranker_score"] = round(float(score), 4)
        return result
    except Exception as e:
        log.warning(f"Reranking failed, using original order: {e}")
        return chunks

