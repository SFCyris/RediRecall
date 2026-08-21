# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.rag — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import hashlib
import os
import re
import threading
import time
import numpy as np
import redis
from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from redisvl.schema import IndexSchema
from . import rag_admin, redis_store, state, textutil
from . import embeddings as _ns_embeddings

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAG INDEX HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def rag_prefix(instance: str) -> str:
    """Key namespace for a RAG instance, e.g. 'rag:default'."""
    return f"rag:{instance}"


# Redis GLOB metacharacters. An instance name reaches SCAN MATCH patterns verbatim, so a
# name containing one of these silently widens the match: an instance literally called "*"
# turns `rag:*:chunk:*` into every chunk of every instance, and resetting or deleting that
# one instance would take all the others with it.
_GLOB_META = str.maketrans({c: "\\" + c for c in "*?[]\\"})


def rag_chunk_glob(instance: str) -> str:
    """SCAN MATCH pattern for one instance's chunk keys, with the name escaped.

    Only for pattern matching — exact key building must keep using ``rag_prefix``,
    where a backslash would become part of the key name.
    """
    return f"rag:{instance.translate(_GLOB_META)}:chunk:*"


def chunk_glob_for_prefix(prefix: str) -> str:
    """Same pattern, for the helpers that are handed a built prefix rather than the
    instance name. Escapes the whole prefix — ``rag:`` contributes no metacharacters,
    so escaping it is a no-op and the instance half is protected either way."""
    return f"{prefix.translate(_GLOB_META)}:chunk:*"


def _get_rag_index(instance: str, rc: redis.Redis) -> SearchIndex:
    """
    Build a redisvl SearchIndex for a RAG instance without creating it in Redis.
    The index schema is declared once here; use .create() / .delete() / .query()
    on the returned object to interact with Redis.

    HNSW parameters:
        M=16            — neighbours per node (higher = better recall, more RAM)
        EF_CONSTRUCTION=200 — build-time beam width (higher = better quality)
    """
    dim    = _ns_embeddings.get_embed_model().get_sentence_embedding_dimension()
    prefix = rag_prefix(instance)
    schema = IndexSchema.from_dict({
        "index": {
            "name":         f"{prefix}:idx",
            "prefix":       f"{prefix}:chunk:",
            "storage_type": "hash",
        },
        "fields": [
            {"name": "text",   "type": "text"},
            # source is a TAG (not TEXT): it holds a whole URL/filename that must
            # not be tokenised. TAG stores the value verbatim, which lets us
            #   • pre-filter a KNN query by source in one round trip
            #       (@source:{*frag*})=>[KNN …]  — see search_rag,
            #   • enumerate distinct sources with FT.AGGREGATE … GROUPBY @source.
            # separator "|" (not the default ",") because commas can appear in a
            # path far more often than a pipe, and each chunk has exactly one source.
            # CASESENSITIVE: TAG fields casefold by default, so a per-document
            # delete of "report.pdf" also matched (and removed) "Report.pdf".
            {"name": "source", "type": "tag",
             "attrs": {"separator": "|", "case_sensitive": True}},
            # sortable so FT.SEARCH … SORTBY chunk_id pages the index directly
            # instead of loading every value at query time (browse endpoint).
            {"name": "chunk_id", "type": "numeric", "attrs": {"sortable": True}},
            # Provenance/lifecycle metadata. doc_id is a stable id for the source
            # document (delete/replace one document without touching the rest);
            # ingested_at enables recency filtering and staleness detection; pos is
            # the chunk's ordinal WITHIN its document, which makes neighbour
            # expansion possible (chunk_id alone is a global counter, not a position).
            # doc_id/pos are written to the hash for provenance but are NOT
            # indexed: nothing queries them (deletes filter on @source, the
            # document list aggregates on @source), and indexing them forced a
            # full DROPINDEX + backfill on every existing install.
            {"name": "ingested_at", "type": "numeric", "attrs": {"sortable": True}},
            # Which registry model produced each vector. Indexed so a corpus that
            # later holds more than one can be partitioned per model, and so
            # "which models are in here?" is a single FT.AGGREGATE rather than a
            # keyspace scan.
            {"name": "emb_model",   "type": "numeric", "attrs": {"sortable": True}},

            {"name": _ns_embeddings.vector_field_for(), "type": "vector",  "attrs": {
                "algorithm":       "hnsw",   # HNSW, not SVS-VAMANA: the latter targets
                "datatype":        "float32", # >10K-vector corpora (10 240-vector training
                "dims":            dim,        # threshold before its compression engages).
                "distance_metric": "cosine",   # At typical instance sizes it adds risk with
                "m":               16,          # no benefit; revisit per-instance if one grows
                "ef_construction": 200,         # past ~10K chunks.
            }},
        ],
    })
    return SearchIndex(schema, redis_client=rc)


_index_ensured: set[str] = set()
# One lock per instance so the schema migration cannot run concurrently with itself.
_index_ensure_locks: dict[str, threading.Lock] = {}

# Bump when _get_rag_index's schema changes in a way that needs a reindex.
#   v2: source TEXT→TAG, chunk_id made SORTABLE (2026-07).
#   v3: added doc_id (tag), ingested_at + pos (sortable numerics) for
#       per-document operations, recency filtering and neighbour expansion (2026-08).
# Deliberately NOT bumped for the doc_id/pos removal. Bumping forces a full
# DROPINDEX + backfill on every existing install, which is the very cost that
# change was made to avoid. Existing v3 indexes keep two unused fields (harmless);
# fresh installs get the lean schema. Both remain fully queryable — nothing in the
# codebase filters or sorts on @doc_id or @pos, and both are still written to the
# hash for provenance.
_RAG_SCHEMA_VERSION = 5

# A keyword-only hit may sit below the cosine threshold and still be the right
# answer, so it is admitted at a fraction of it. Below that it is tail noise:
# one common term matching an otherwise unrelated document.
_LEXICAL_FLOOR_RATIO = 0.7

# HNSW search breadth. RediSearch defaults EF_RUNTIME to 10, which on a 57,805-
# vector index measured recall@10 of 0.839 against exact brute force — one query
# in six returned nothing from the true top-10. Raising it to 128 measured
# recall 1.000 AND was faster (0.81 ms median / 0.98 ms p95, versus 1.19 / 3.67
# at the default): a wider beam converges more predictably than a narrow one that
# wanders. Cost grows again beyond ~256, so this is near the sweet spot.
_HNSW_EF_RUNTIME = int(os.environ.get("REDIRECALL_HNSW_EF_RUNTIME", "128"))

# Backstop for the per-document delete loop. It re-queries at offset 0 by design
# (each batch is deleted before the next search), so any state where DEL does not
# retract the index entry would otherwise spin a worker thread forever.
_MAX_DELETE_BATCHES = 10_000

def _migrate_vector_field(instance: str, rc: redis.Redis) -> int:
    """Move each chunk's vector from the legacy "embedding" field to the
    width-named one, so a v4-or-older corpus stays searchable after the rename.

    Idempotent: a chunk that already carries the new field is skipped, and the
    legacy field is only deleted once the new one is written. Returns the number
    of chunks moved.
    """
    field = _ns_embeddings.vector_field_for()
    if field == "embedding":
        return 0                       # unregistered model — nothing renamed
    moved, batch = 0, []
    try:
        for key in rc.scan_iter(rag_chunk_glob(instance), count=500):
            batch.append(key)
            if len(batch) >= 500:
                moved += _migrate_vector_batch(rc, batch, field); batch = []
        if batch:
            moved += _migrate_vector_batch(rc, batch, field)
    except Exception as e:
        log.warning(f"vector-field migration for '{instance}' stopped early: {e}")
    if moved:
        log.info(f"'{instance}': moved {moved} vectors to field '{field}'")
    return moved


def _migrate_vector_batch(rc: redis.Redis, keys: list, field: str) -> int:
    """One pipelined pass: read legacy vectors, write them under the new name."""
    pipe = rc.pipeline(transaction=False)
    for k in keys:
        pipe.hmget(k, "embedding", field)
    rows = pipe.execute()
    write = rc.pipeline(transaction=False)
    n = 0
    for k, (legacy, current) in zip(keys, rows):
        if current or not legacy:
            continue                   # already migrated, or nothing to move
        write.hset(k, field, legacy)
        write.hdel(k, "embedding")
        n += 1
    if n:
        write.execute()
    return n


def ensure_rag_index(instance: str, rc: redis.Redis | None = None):
    """
    Ensure the RediSearch index for a RAG instance exists with the CURRENT schema.

    A per-instance marker key (rag:<instance>:schema_ver) records the schema
    version the index was built with. When it is behind _RAG_SCHEMA_VERSION (or
    absent — e.g. an index built by an older RediRecall), the index is recreated
    with `overwrite=True`, which drops the index definition WITHOUT the DD flag —
    so the chunk HASHes are preserved and RediSearch reindexes them in the
    background. The marker is then advanced so the reindex happens at most once
    per version, not on every startup.

    In-process `_index_ensured` short-circuits repeat calls within one process.
    """
    if instance in _index_ensured:
        return
    rc = rc or redis_store.r()
    ver_key = f"rag:{instance}:schema_ver"
    # _index_ensured is only set at the END, so concurrent first-callers all ran
    # the slow path. Harmless when it was just FT.CREATE; not harmless now that
    # it can move 57k vectors — the migration ran twice on a real corpus.
    with _index_ensure_locks.setdefault(instance, threading.Lock()):
        if instance in _index_ensured:
            return                      # won by another thread while we waited
        _ensure_rag_index_locked(instance, rc, ver_key)


def _ensure_rag_index_locked(instance: str, rc: redis.Redis, ver_key: str) -> None:
    """The slow path of ensure_rag_index, serialised per instance."""
    try:
        raw = rc.get(ver_key)
        have_ver = int(raw) if raw else 0
    except Exception:
        have_ver = 0
    try:
        if have_ver >= _RAG_SCHEMA_VERSION:
            # Up to date — create only if the index is somehow missing (no-op otherwise).
            _get_rag_index(instance, rc).create(overwrite=False)
        else:
            # v5 renamed the vector hash field from "embedding" to
            # "embedding_<dim>". Recreating the index alone would leave every
            # existing chunk with its vector under the OLD name, so the new index
            # would find nothing and the whole corpus would go dark with no error.
            # Move the field first, then rebuild.
            if have_ver < 5:
                _migrate_vector_field(instance, rc)
            # New or outdated schema: recreate the index definition, keeping the
            # chunk data (overwrite drops the index only, not the documents).
            _get_rag_index(instance, rc).create(overwrite=True)
            try:
                rc.set(ver_key, _RAG_SCHEMA_VERSION)
            except Exception:
                pass
            log.info(f"RAG index for '{instance}' built at schema v{_RAG_SCHEMA_VERSION}")
    except Exception as e:
        msg = str(e)
        # "Index already exists" is not an error — the index is present, which is what we want.
        if "already exists" not in msg.lower():
            log.warning(f"RAG index creation for '{instance}': {e}")
    _index_ensured.add(instance)


def next_chunk_id(instance: str, reserve: int = 1, rc: redis.Redis | None = None) -> int:
    """
    Atomically reserve `reserve` sequential chunk IDs using a Redis INCRBY counter.

    This is O(1) — a single Redis round-trip — whereas the old approach of
    scanning all chunk keys was O(N) and very slow for large instances.

    Returns the first ID in the reserved range, so caller can use
    [start, start+1, ..., start+reserve-1].
    """
    rc = rc or redis_store.r()
    counter_key = f"rag:{instance}:chunk_counter"
    new_val = int(rc.incrby(counter_key, reserve))
    return new_val - reserve   # return the start of the reserved range


def add_chunks(instance: str, chunks: list[dict], rc: redis.Redis | None = None):
    """
    Store a batch of text chunks in Redis with their embeddings.

    Optimisations:
    - All texts are embedded in ONE model call (batch inference).
    - All Redis writes are pipelined (one round-trip for the whole batch).
    """
    if not chunks:
        return

    rc = rc or redis_store.r()
    ensure_rag_index(instance, rc)
    prefix = rag_prefix(instance)

    # Batch embed — one model call for the entire chunk list
    texts = [ch["text"] for ch in chunks]
    embeddings = _ns_embeddings.embed_batch(texts)   # shape: (N, dim)

    pipe = rc.pipeline(transaction=False)
    for ch, emb in zip(chunks, embeddings):
        key = f"{prefix}:chunk:{ch['id']}"
        src = ch.get("source", "")
        pipe.hset(key, mapping={
            "text":        ch["text"].encode(),
            "source":      src.encode(),
            "chunk_id":    str(ch["id"]),
            "doc_id":      doc_id_for(src),
            "ingested_at": str(int(ch.get("ingested_at", time.time()))),
            "pos":         str(int(ch.get("pos", 0))),
            # Which registry model produced this vector. Per chunk, not per index,
            # so an instance can later hold vectors from several models at once.
            "emb_model":   str(_ns_embeddings.embedding_id_for()),
            _ns_embeddings.vector_field_for(): emb.astype(np.float32).tobytes(),
        })
    pipe.execute()


def _decode(v) -> str:
    """Decode a bytes value to str; pass strings through unchanged."""
    return v.decode() if isinstance(v, bytes) else (v or "")


def number_chunks(chunks: list[dict]) -> list[dict]:
    """Stamp each chunk with the citation number the model will be shown, in place.

    The ``[1]…[k]`` markers in an answer are positions in the list handed to the prompt,
    so the number is a property of that list and nothing else. Recording it on the chunk
    makes it survive into the websocket payload and the stored turn, which is what lets
    the RAG inspector label a chunk with the same number the answer cites. Deriving it
    again in the browser cannot work: the inspector re-orders by relevance, and a cached
    or restored payload has no ordering guarantee at all.
    """
    for i, c in enumerate(chunks, 1):
        c["n"] = i
    return chunks


def build_context_prompt(chunks: list[dict]) -> str:
    """Render retrieved chunks into a system-prompt section.

    Three behaviours the previous flat blob lacked:

    * **Relevance gate.** Retrieval is a similarity search, and a weak match can
      clear the floor on vocabulary overlap alone (a real case: "Beginning of Old
      MacDonald" pulled a 43% Linux man-page chunk, and the model summarised the
      man page and abstained instead of answering the song). The model is told the
      context is machine-retrieved and possibly irrelevant, to judge it first, and
      to IGNORE it — not narrate it — when it does not bear on the question. Each
      chunk carries its match score so the model can calibrate that judgement.
    * **Citations.** Chunks are numbered ``[1]…[k]`` and the model is told to mark
      each claim with the number it came from, so a reader can trace a specific
      sentence back to a specific chunk instead of being handed the whole set.
    * **Abstention.** With no chunks the model was previously given no instruction
      at all and answered from parametric knowledge — indistinguishable from a
      grounded answer. It is now told explicitly to say so first.
    """
    if not chunks:
        return (
            "\n\nNo relevant context was found in the knowledge base for this question. "
            "Say so in one short sentence before answering, then answer from general "
            "knowledge and make clear that the answer is not grounded in the user's documents."
        )

    def _pct(c: dict) -> str:
        s = c.get("relevance", c.get("score"))
        try:
            return f", match {round(float(s) * 100)}%" if s is not None else ""
        except (TypeError, ValueError):
            return ""

    numbered = "\n\n".join(
        f"[{c.get('n', i)}] (source: {c.get('source', 'unknown')}{_pct(c)})\n{c.get('text', '')}"
        for i, c in enumerate(chunks, 1)
    )
    return (
        "\n\nThe numbered context below was retrieved from the user's knowledge base "
        "by automatic similarity search — it may or may not be relevant to the "
        "question (low match percentages are often vocabulary coincidences). Judge "
        "that first:\n"
        "- If none of it bears on the question, IGNORE it entirely: do not describe "
        "or summarise it, and do not answer with what the context lacks. Instead say "
        "in one short sentence that the knowledge base has nothing relevant, then "
        "answer the question from general knowledge.\n"
        "- If only some chunks are relevant, use those and silently ignore the rest.\n"
        "- When you do use the context, cite it by appending the chunk number in "
        "square brackets to the sentence it supports — for example: \"Streams are "
        "append-only [2].\" Cite only what you actually used, and use several markers "
        "when a sentence draws on more than one. If the context covers the question "
        "only partly, ground what you can and say which part goes beyond the "
        "user's documents.\n\n"
        f"{numbered}"
    )


def doc_id_for(source: str) -> str:
    """Stable id for a source document.

    A hash rather than the raw source because the value is stored in a TAG field:
    URLs and paths contain the TAG separator and other reserved characters, and a
    fixed-width hex id keeps per-document queries (delete / replace / list) simple
    and injection-free.
    """
    return hashlib.sha256((source or "").encode()).hexdigest()[:16]


# Every RediSearch TAG special character, INCLUDING the "|" tag-union operator
# (which is also this schema's field separator), "*", and the ASCII control
# range (a bare CR/FF/VT in the query also trips the parser). redisvl's own
# escaper leaves "|" and control chars unescaped, so we escape the full set.
_TAG_ESCAPE_RE = re.compile(r'([\\,.<>{}\[\]"\'`:;!@#$%^&*()\-+=~|/?\x00-\x1f\x7f ])')

def _source_infix_filter(fragment: str) -> "str | None":
    """Build a RediSearch TAG filter matching chunks whose ``source`` CONTAINS
    ``fragment`` as a LITERAL substring: ``@source:{*<escaped>*}``.

    Every TAG special char in the fragment is backslash-escaped, so the fragment
    is matched literally — it can neither error the parser as a multi-wildcard
    term (a fragment with ``*``) nor be reinterpreted as a tag union (``|``).
    Only the two surrounding ``*`` wildcards stay active. NUL is stripped rather
    than escaped (the query parser rejects it even escaped). Returns None for an
    empty fragment (→ unfiltered query).
    """
    fragment = (fragment or "").replace("\x00", "")
    if not fragment:
        return None
    esc = _TAG_ESCAPE_RE.sub(r'\\\1', fragment)
    return f"@source:{{*{esc}*}}"


# Instances already warned about a dead BM25 leg (one log line, not one per query).
_bm25_leg_warned: set[str] = set()

# Instances already warned about an embedding-dimension mismatch (same rationale).
_dim_mismatch_warned: set[str] = set()


def _is_vector_dim_error(exc: Exception) -> bool:
    """True when a query failed because the query vector's width doesn't match
    the index — i.e. the embedding model changed but the index was not rebuilt."""
    # Redis 8.x actually says: "query vector blob size (3072) does not match
    # index's expected size (1536)" — no occurrence of the word "dimension", which
    # an earlier version of this predicate required, so it never matched.
    s = str(exc).lower()
    if "blob size" in s or "expected size" in s:
        return True
    if "vector" in s and "size" in s and ("match" in s or "invalid" in s):
        return True
    return ("dimension" in s or "dim" in s.split()) and (
        "match" in s or "expected" in s or "invalid" in s or "blob" in s)


def _warn_if_index_empty_but_data_exists(instance: str, rc: redis.Redis) -> None:
    """Explain an empty index that still has stored chunks.

    After the embedding model changes, RediSearch rebuilds the index at the new
    dimension and simply *skips* every existing vector — no exception is raised,
    the query just returns nothing. That is indistinguishable from "no match" to
    the caller, so state it explicitly, once per instance.
    """
    warn_if_vectors_are_from_another_model(instance, rc)
    if instance in _dim_mismatch_warned:
        return
    try:
        info = rc.execute_command("FT.INFO", f"{rag_prefix(instance)}:idx")
        d = {_decode(info[i]): info[i + 1] for i in range(0, len(info) - 1, 2)}
        indexed = int(d.get("num_docs", 0) or 0)
        if indexed > 0:
            return                       # genuinely just a no-match query
        # A rebuild in progress also reports num_docs 0 while chunk keys exist.
        # Warning there is wrong AND harmful: it memoises the instance, which
        # suppressed the real diagnostic for the rest of the process's life.
        try:
            if float(d.get("percent_indexed", 1) or 1) < 1:
                return
        except (TypeError, ValueError):
            pass
        # Only existence matters, so stop at the first key instead of counting the
        # whole keyspace, and memoise the clean result too — without that, a
        # genuinely empty instance re-scanned on every single query.
        if next(rc.scan_iter(rag_chunk_glob(instance), count=200), None) is None:
            _dim_mismatch_warned.add(instance)
            return                       # empty instance — nothing to explain
        stored = sum(1 for _ in rc.scan_iter(rag_chunk_glob(instance), count=200))
        _dim_mismatch_warned.add(instance)
        log.error(
            f"RAG DISABLED for '{instance}': {stored} chunks are stored but 0 are indexed. "
            f"This is the signature of an embedding-model change — the index was rebuilt "
            f"for {state._config.get('embedding', {}).get('model', '?')} and the existing vectors "
            f"have a different dimension, so none of them indexed. Re-ingest this instance, "
            f"or switch the embedding model back to the one it was built with."
        )
    except Exception:
        pass


_emb_mismatch_warned: set[str] = set()


def warn_if_vectors_are_from_another_model(instance: str, rc: redis.Redis) -> None:
    """Warn when an index holds vectors the ACTIVE model did not produce.

    The dangerous case is a same-width swap — MiniLM and e5-small are both 384d,
    so stale vectors stay structurally valid and Redis raises nothing. Queries
    then return confident nonsense. A width change fails loudly on its own; this
    covers the one that does not.
    """
    if instance in _emb_mismatch_warned:
        return
    active = _ns_embeddings.embedding_id_for()
    try:
        res = rc.execute_command(
            "FT.AGGREGATE", f"{rag_prefix(instance)}:idx", "*",
            "GROUPBY", "1", "@emb_model",
            "REDUCE", "COUNT", "0", "AS", "n", "LIMIT", "0", "20")
        found = set()
        for row in res[1:]:
            d = {_decode(row[i]): _decode(row[i + 1]) for i in range(0, len(row) - 1, 2)}
            try:
                found.add(int(d.get("emb_model", -1)))
            except (TypeError, ValueError):
                found.add(-1)
    except Exception:
        return          # pre-v4 index, or no index yet — nothing to compare
    # -1 is "provenance not recorded" (chunks written before emb_model existed),
    # NOT "a different model". Treating it as a mismatch fires this warning on
    # every pre-existing install, whose vectors are usually perfectly correct.
    conflicting = {i for i in found if i >= 0} - {active}
    if conflicting:
        _emb_mismatch_warned.add(instance)
        others = ", ".join(_ns_embeddings.EMBEDDING_MODELS.get(i, {}).get("repo", f"id {i}")
                           for i in sorted(conflicting))
        log.warning("=" * 70)
        log.warning(f"'{instance}' holds vectors from {others}, but the active model is "
                    f"{_ns_embeddings.EMBEDDING_MODELS.get(active, {}).get('repo', '?')}.")
        log.warning("Those chunks are searched with a query vector from a DIFFERENT model.")
        log.warning("Results will be wrong without any error. Re-ingest this instance.")
        log.warning("=" * 70)


def search_rag(
    instance: str,
    query: str,
    top_k: int = 5,
    threshold: float = 0.0,
    rc: redis.Redis | None = None,
    hybrid: bool = True,
    query_vec: "np.ndarray | None" = None,
    source_filter: str = "",
) -> list[dict]:
    """
    Search a RAG instance and return the top-K most relevant chunks.

    When ``hybrid=True`` (default) the search combines two strategies via
    Reciprocal Rank Fusion (RRF):

      1. **Vector KNN** — semantic similarity via redisvl VectorQuery (HNSW cosine).
         Catches paraphrases and related concepts.
      2. **BM25 full-text** (``SCORER BM25STD``) — exact/near-exact keyword
         matching. Catches precise terms a paraphrase-tuned embedding misses.

    RRF formula: each result gets 1/(K+rank) for every list it appears in.
    K=60 is the standard constant that prevents high ranks from dominating.
    Results are then re-ranked by combined RRF score. A chunk that matched the
    full-text leg is kept regardless of the cosine ``threshold`` (it was
    selected lexically, so the cosine bar is the wrong gate); only vector-only
    hits are threshold-filtered.

    ``source_filter`` (substring of a chunk's source) is pushed INTO both legs
    as a TAG pre-filter — ``(@source:{*frag*})`` — so the KNN and BM25 searches
    run within the matching sources. This replaces an earlier post-filter that
    ran after the top-K cut and silently returned nothing when the scoped source
    lost the global ranking race.

    When ``hybrid=False`` only the vector search is performed.
    """
    # redis_store.r(), not a bare r(): `r` is a loop variable further down this same
    # function, which makes Python treat it as local for the whole body — so the bare
    # call raised UnboundLocalError on every caller that left `rc` unset.
    rc     = rc or redis_store.r()
    prefix = rag_prefix(instance)
    idx_name = f"{prefix}:idx"
    # Make sure the index exists with the current schema (source as TAG) before
    # we build a TAG pre-filter against it. Cached after the first call.
    ensure_rag_index(instance, rc)
    # Use a pre-computed query vector (e.g. from HyDE) if provided, otherwise embed the query.
    q_emb = query_vec if query_vec is not None else _ns_embeddings.embed(query, is_query=True).astype(np.float32)

    # source_filter → TAG infix-wildcard pre-filter, fully escaped so the value
    # is matched as a literal substring (see _source_infix_filter). A raw filter
    # string; redisvl emits DIALECT 2 for the combined KNN query.
    src_expr = _source_infix_filter(source_filter)

    try:
        # ── 1. Vector KNN search via redisvl VectorQuery ──────────────────────
        # Fetch top_k*2 so that after RRF merging we still have enough candidates.
        fetch_k = top_k * 2 if hybrid else top_k
        vq = VectorQuery(
            vector=q_emb.tolist(),
            vector_field_name=_ns_embeddings.vector_field_for(),
            ef_runtime=_HNSW_EF_RUNTIME,
            return_fields=["text", "source"],
            num_results=fetch_k,
            filter_expression=src_expr,   # None → unfiltered KNN
        )
        idx = _get_rag_index(instance, rc)
        raw_vec = idx.query(vq)   # list[dict]: id, text, source, vector_distance
        if not raw_vec:
            _warn_if_index_empty_but_data_exists(instance, rc)

        # vector_distance is cosine DISTANCE (0=identical); convert to similarity
        vec_rows: list[dict] = []
        for row in raw_vec:
            vec_rows.append({
                "_key":       row.get("id", ""),
                "text":       _decode(row.get("text", "")),
                "source":     _decode(row.get("source", "")),
                "_vec_score": round(1.0 - float(row.get("vector_distance", 1.0)), 4),
            })

        # ── 2. BM25 full-text search (hybrid mode only) ───────────────────────
        bm25_rows: list[dict] = []
        if hybrid:
            kws = textutil._keywords_for_bm25(query)
            if kws:
                text_q = " | ".join(kws)
                # Scope the lexical leg to the same sources as the vector leg.
                # src_expr is "@source:{*frag*}"; space = AND with the text match.
                text_query = f"@text:({text_q})"
                if src_expr is not None:
                    text_query = f"{src_expr} {text_query}"
                try:
                    txt_res = rc.execute_command(
                        "FT.SEARCH", idx_name,
                        text_query,
                        "SCORER", "BM25STD",   # standard BM25 ranking (Redis 8.x)
                        "WITHSCORES",          # needed: a keyword-only hit has no
                                               # cosine, so BM25 is its only signal
                        "RETURN", "2", "text", "source",
                        "LIMIT", "0", str(fetch_k),
                        "DIALECT", "2",
                    )
                    # Parse raw FT.SEARCH response (key, [field, val, ...], ...)
                    # WITHSCORES widens the reply stride to (key, score, fields).
                    items = txt_res[1:]
                    for i in range(0, len(items) - 2, 3):
                        key = _decode(items[i])
                        try:
                            bm25 = float(_decode(items[i + 1]))
                        except (TypeError, ValueError):
                            bm25 = 0.0
                        fields = items[i + 2]
                        d: dict = {"_key": key, "_vec_score": 0.0, "_bm25": bm25}
                        for j in range(0, len(fields), 2):
                            d[_decode(fields[j])] = _decode(fields[j + 1])
                        bm25_rows.append(d)
                except Exception as te:
                    # Degrade to vector-only, but say so once per instance. A
                    # silent pass here hides a total loss of the lexical leg —
                    # notably on a RediSearch older than 8.0, which rejects
                    # "SCORER BM25STD" outright, so every keyword-only answer
                    # becomes unreachable with no signal that hybrid is off.
                    if instance not in _bm25_leg_warned:
                        _bm25_leg_warned.add(instance)
                        log.warning(
                            f"Hybrid text leg unavailable for '{instance}' — "
                            f"degrading to vector-only search: {te}"
                        )

        # ── 3. RRF merge and re-rank ─────────────────────────────────────────
        K_RRF  = 60
        bm25_keys = {row["_key"] for row in bm25_rows}   # chunks that matched lexically
        scores: dict[str, dict] = {}
        for rank, row in enumerate(vec_rows):
            key = row["_key"]
            scores.setdefault(key, {"row": row, "rrf": 0.0})
            scores[key]["rrf"] += 1.0 / (K_RRF + rank + 1)

        for rank, row in enumerate(bm25_rows):
            key = row["_key"]
            if key in scores:
                scores[key]["rrf"] += 1.0 / (K_RRF + rank + 1)
            else:
                scores[key] = {"row": row, "rrf": 1.0 / (K_RRF + rank + 1)}

        ranked = sorted(scores.values(), key=lambda x: x["rrf"], reverse=True)[:top_k]

        # A keyword-only hit has no vector_distance, so RediSearch reports nothing
        # for it and it used to display as "0.0%". Fetch its stored embedding and
        # compute the real cosine against the query, so every chunk is scored on
        # ONE scale. Normalising BM25 instead is not comparable: its top hit is
        # always 1.0, which buries every vector hit beneath it.
        missing = [r for r in bm25_rows if not r.get("_vec_score")]
        if missing:
            try:
                pipe = rc.pipeline(transaction=False)
                for r in missing:
                    pipe.hget(r["_key"], _ns_embeddings.vector_field_for())
                blobs = pipe.execute()
                qv = np.asarray(q_emb, dtype=np.float32)
                qn = float(np.linalg.norm(qv)) or 1.0
                for r, blob in zip(missing, blobs):
                    if not blob:
                        continue
                    cv = np.frombuffer(blob, dtype=np.float32)
                    if cv.size != qv.size:
                        continue
                    cn = float(np.linalg.norm(cv)) or 1.0
                    r["_vec_score"] = round(float(np.dot(qv, cv)) / (qn * cn), 4)
            except Exception as ve:
                log.debug(f"cosine backfill for lexical hits failed: {ve}")

        raw_results = [
            {
                "text":    item["row"].get("text", ""),
                "source":  item["row"].get("source", ""),
                "score":   float(item["row"].get("_vec_score", 0.0)),
                "bm25":    float(item["row"].get("_bm25", 0.0)),
                # Combined RRF rank score — the authoritative ordering (see below).
                # search_rag_parallel fuses across instances on this, not on the
                # cosine `score`, so keyword-only hits (cosine 0) aren't buried.
                "_fused":  float(item["rrf"]),
                "lexical": item["row"]["_key"] in bm25_keys,
            }
            for item in ranked
        ]

        # Threshold gates the cosine score — but a chunk that matched the BM25
        # leg was selected lexically, so keep it regardless. Without this, every
        # keyword-only hit (cosine score 0) is dropped and hybrid collapses to
        # vector-only. source_filter is already applied in-query (both legs).
        # A lexical hit is exempt from the cosine threshold because it was selected
        # by term match, not by distance — but the exemption used to be unbounded,
        # so the BM25 tail (a single common term in an unrelated document) arrived
        # with cosine 0.0 and went straight into the prompt. Require a real share of
        # the best keyword score instead.
        # Lexical hits are still exempt from the full cosine threshold — they were
        # selected by term match, and a keyword answer can sit just under it. But
        # the exemption is now bounded by a real cosine floor, so the BM25 tail (a
        # single common term in an unrelated document) no longer reaches the prompt.
        lex_floor = threshold * _LEXICAL_FLOOR_RATIO
        kept = [
            c for c in raw_results
            if c["score"] >= threshold or (c["lexical"] and c["score"] >= lex_floor)
        ]
        for c in kept:
            c["relevance"] = round(c["score"], 4)
        kept.sort(key=lambda c: (c["relevance"], c.get("_fused", 0.0)), reverse=True)
        results = [{k: v for k, v in c.items() if k != "lexical"} | {"lexical": c["lexical"]}
                   for c in kept]
        state._record_rag_stats(instance, results, raw_results)
        return results

    except Exception as e:
        # Auto-recover: if the FT index was dropped (e.g. Redis restart without
        # RDB persistence), recreate it and retry the query once — no recursion.
        if "no such index" in str(e).lower():
            log.warning(f"RAG index missing for '{instance}', recreating and retrying…")
            _index_ensured.discard(instance)
            ensure_rag_index(instance, rc)
            try:
                idx = _get_rag_index(instance, rc)
                fetch_k = top_k * 2 if hybrid else top_k
                q_emb2 = (query_vec if query_vec is not None
                          else _ns_embeddings.embed(query, is_query=True).astype(np.float32))
                vq2 = VectorQuery(
                    vector=q_emb2.tolist(),
                    vector_field_name=_ns_embeddings.vector_field_for(),
                    ef_runtime=_HNSW_EF_RUNTIME,
                    return_fields=["text", "source"],
                    num_results=fetch_k,
                    filter_expression=src_expr,   # keep source scoping on retry
                )
                raw2 = idx.query(vq2)
                results2 = [
                    {
                        "text":   _decode(row.get("text", "")),
                        "source": _decode(row.get("source", "")),
                        "score":  round(1.0 - float(row.get("vector_distance", 1.0)), 4),
                        # Same RRF-from-rank scale as the main vector leg, so a
                        # recovered instance fuses correctly in the parallel merge
                        # instead of sorting to the bottom (missing _fused → 0.0).
                        "_fused": 1.0 / (60 + rank + 1),
                    }
                    for rank, row in enumerate(raw2)
                ]
                # Vector-only recovery path: threshold gates the cosine score.
                results2 = [c for c in results2 if c["score"] >= threshold]
                state._record_rag_stats(instance, results2, results2)
                return results2
            except Exception as e2:
                log.warning(f"RAG search still failing for '{instance}' after index recreate: {e2}")
        elif _is_vector_dim_error(e):
            # The query vector no longer matches the index — almost always because
            # the embedding model was changed without re-indexing. Say so loudly
            # and once per instance: the generic path would return [] and the user
            # would see RAG "working" but never retrieving anything.
            if instance not in _dim_mismatch_warned:
                _dim_mismatch_warned.add(instance)
                log.error(
                    f"RAG DISABLED for '{instance}': the index was built for a different "
                    f"embedding dimension than the current model "
                    f"({state._config.get('embedding', {}).get('model', '?')}) produces. "
                    f"Re-ingest this instance, or switch the embedding model back. ({e})"
                )
        else:
            log.warning(f"RAG search skipped for '{instance}': {e}")
        state._record_rag_stats(instance, [], [])
        return []


async def search_rag_parallel(
    instances: list[str],
    query: str,
    top_k: int = 5,
    threshold: float = 0.0,
    hybrid: bool = True,
    query_vec: "np.ndarray | None" = None,
    source_filter: str = "",
) -> list[dict]:
    """
    Search multiple RAG instances concurrently and return a merged, score-sorted list.

    Each instance may live on a different Redis server (resolved via rc_for_instance).
    Only enabled instances are queried.  Results from all instances are merged and
    re-ranked by similarity score so the top_k best chunks are returned regardless
    of which instance they came from.

    The `instance` key is added to each chunk so the caller knows the origin.
    """
    loop = asyncio.get_event_loop()

    # Filter to enabled + reachable instances only
    enabled: list[str] = []
    for inst in instances:
        try:
            meta, ep = await rag_admin._rag_meta_cached_async(inst)   # cached; miss resolves off-loop
            enabled_flag = True
            if meta:
                enabled_flag = meta.get("enabled", True)
                # Skip if the owning endpoint is known to be offline
                if not state._endpoint_health.get(ep, True):
                    continue
            if enabled_flag:
                enabled.append(inst)
        except Exception:
            pass  # skip unreachable instances gracefully

    if not enabled:
        return []

    # Embed the query ONCE and reuse the vector across every instance. Without this,
    # each instance's search_rag independently re-embeds the identical query string
    # (N redundant local encodes + executor contention). HyDE already supplies
    # query_vec; this covers the common HyDE-off path. Local model — saves CPU, not tokens.
    if query_vec is None:
        query_vec = await loop.run_in_executor(
            None, lambda: _ns_embeddings.embed(query, is_query=True).astype(np.float32))

    async def _search_one(inst: str) -> list[dict]:
        """Run a single synchronous search in a thread-pool so searches are parallel."""
        rc = rag_admin.rc_for_instance(inst)
        results = await loop.run_in_executor(
            None, search_rag, inst, query, top_k, threshold, rc, hybrid, query_vec, source_filter
        )
        # Tag each chunk with its origin instance
        for c in results:
            c["instance"] = inst
        return results

    # Fan out to all enabled instances simultaneously
    per_instance = await asyncio.gather(*[_search_one(i) for i in enabled])

    # Merge and keep the global top_k. Rank on the combined RRF score (_fused),
    # NOT the cosine `score`: sorting by cosine would push every keyword-only hit
    # (cosine 0) to the bottom and cut it, silently undoing the hybrid
    # keyword-exemption across instances. RRF scores share a scale across
    # instances (each ≈ Σ 1/(60+rank)), so they fuse sensibly. Cosine breaks ties.
    merged: list[dict] = []
    for results in per_instance:
        merged.extend(results)
    merged.sort(key=lambda c: (c.get("relevance", c.get("score", 0.0)),
                               c.get("_fused", 0.0)), reverse=True)
    return merged[:top_k]


