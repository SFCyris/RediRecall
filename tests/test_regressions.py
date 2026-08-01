# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests, one per fixed defect.

Each test is named for the issue it locks down and states, in its docstring, the
behaviour that was wrong. When you fix a bug, add a test here that fails against
the old code and passes against the new one — that is the whole point of the
file. See tests/README.md.
"""
import asyncio
import json
import os
import time

import pytest


# ── #5 chunking: the token guard must not shred the configured chunk size ─────

def test_issue5_chunk_size_stays_near_configured(app_module):
    """Guarded chunks kept only ~40% of the configured size on ordinary prose.

    The token guard runs after word-based windowing. On text that tokenises
    close to 1 token/word it must leave chunks alone rather than re-cutting them.
    """
    m = app_module
    text = "The quick brown fox jumps over the lazy dog. " * 2000
    chunks = m.chunk_text(text, size=180, overlap=32)
    words = [len(c.split()) for c in chunks]
    median = sorted(words)[len(words) // 2]
    assert median >= 180 * 0.75, (
        f"median chunk is {median} words, configured 180 — the guard is over-cutting"
    )


def test_issue5_overlap_is_preserved(app_module):
    """Zero-overlap boundaries rose from 56% to 89% once the guard was added."""
    m = app_module
    text = " ".join(f"sentence{i} about databases and storage systems." for i in range(4000))
    chunks = m.chunk_text(text, size=180, overlap=32)
    joined = 0
    for a, b in zip(chunks, chunks[1:]):
        tail = set(a.split()[-32:])
        if tail & set(b.split()[:32]):
            joined += 1
    ratio = joined / max(1, len(chunks) - 1)
    assert ratio >= 0.5, f"only {ratio:.0%} of boundaries carry overlap"


def test_token_guard_holds_across_shapes(app_module):
    """No chunk may exceed the encoder limit, whatever the input looks like.

    Covers the three shapes that defeated the first implementation: a
    whitespace-free run (collapsed to one [UNK], so it counted as 3 tokens),
    CJK (no whitespace to split on) and a very large real document.
    """
    m = app_module
    limit = m._model_token_limit()
    shapes = {
        "base64":  "QUJDRA" * 20000,
        "minified": "function(a,b){return a+b}" * 2000,
        "chinese": "这是一个关于数据库的文档。" * 300,
        "japanese": "これはデータベースに関する文書です。" * 350,
        "prose":   "The quick brown fox jumps over the lazy dog. " * 4000,
    }
    for name, text in shapes.items():
        chunks = m.chunk_text(text, size=180, overlap=32)
        worst = max(m.count_tokens(c) for c in chunks)
        assert worst <= limit, f"{name}: {worst} tokens > limit {limit}"


def test_long_runs_do_not_collapse_to_a_single_unk(app_module):
    """A run over max_input_chars_per_word became one [UNK]: 200 KB counted as 3."""
    m = app_module
    assert m.count_tokens("A" * 5000) > 100


# ── #6 sessions must not be evicted from the sidebar ─────────────────────────

def test_issue6_unused_session_ids_are_not_remembered(app_module):
    """Ids were recorded on session creation, so empty sessions consumed the cap
    of 200 and pushed real conversations out of the list."""
    src = (app_module.__file__.replace("main.py", "index.html"))
    html = open(src, encoding="utf-8").read()
    start = html.index("function newSession(")
    body = html[start:start + 900]
    assert "_rememberSessionId" not in body, (
        "newSession() still records the id; it must be recorded on first send"
    )


# ── #7 per-document delete must match the exact source ───────────────────────

def test_issue7_delete_is_case_sensitive(app_module, clean_redis):
    """RediSearch TAG fields casefold, so deleting report.pdf also took Report.pdf."""
    rc = clean_redis
    prefix = rc.key("t7:chunk:")
    idx = rc.key("t7:idx")
    rc.execute_command(
        "FT.CREATE", idx, "ON", "HASH", "PREFIX", "1", prefix,
        "SCHEMA", "source", "TAG", "SEPARATOR", "|", "CASESENSITIVE", "text", "TEXT")
    try:
        rc.hset(prefix + "1", mapping={"source": "report.pdf", "text": "lower"})
        rc.hset(prefix + "2", mapping={"source": "Report.pdf", "text": "upper"})
        time.sleep(0.4)
        # '.' is a TAG metacharacter; production escapes via _TAG_ESCAPE_RE.
        esc = app_module._TAG_ESCAPE_RE.sub(r"\\\1", "report.pdf")
        res = rc.execute_command("FT.SEARCH", idx, f"@source:{{{esc}}}", "RETURN", "1", "text")
        assert res[0] == 1, f"exact-case query matched {res[0]} docs, expected 1"
        esc_up = app_module._TAG_ESCAPE_RE.sub(r"\\\1", "Report.pdf")
        res_up = rc.execute_command("FT.SEARCH", idx, f"@source:{{{esc_up}}}", "RETURN", "1", "text")
        assert res_up[0] == 1 and res_up[1] != res[1], "the two cases resolve to the same doc"
    finally:
        rc.execute_command("FT.DROPINDEX", idx)


def test_issue7_schema_marks_source_casesensitive(app_module):
    """The live schema must carry CASESENSITIVE, not just the test's local index."""
    import inspect
    src = inspect.getsource(app_module._get_rag_index)
    assert "casesensitive" in src.lower(), "source TAG is still case-folding"


# ── #9 feedback endpoint must be bounded ─────────────────────────────────────

def test_issue9_feedback_limit_cannot_be_bypassed(app_module):
    """items[-max(0, limit):] is items[0:] for limit<=0 — the whole store leaked."""
    m = app_module
    m._feedback = [{"i": i} for i in range(50)]
    for bad in (0, -5, -1):
        got = m.api_feedback_list(limit=bad)
        assert len(got["items"]) < 50, f"limit={bad} returned {len(got['items'])} of 50"
    assert len(m.api_feedback_list(limit=3)["items"]) == 3


def test_issue9_feedback_payload_is_size_capped(app_module):
    """An unbounded POST body let one request bloat the on-disk store."""
    m = app_module
    assert hasattr(m, "_MAX_FEEDBACK_FIELD"), "no per-field cap defined"
    assert m._MAX_FEEDBACK_FIELD <= 20000


# ── #12 the documented filter value must actually work ───────────────────────

def test_issue12_feedback_value_filter_accepts_up_and_down(app_module):
    """The docstring advertised value=down but the client stores 1/-1."""
    m = app_module
    m._feedback = [{"value": 1, "id": "a"}, {"value": -1, "id": "b"}, {"value": 1, "id": "c"}]
    assert len(m.api_feedback_list(limit=10, value="down")["items"]) == 1
    assert len(m.api_feedback_list(limit=10, value="up")["items"]) == 2
    assert len(m.api_feedback_list(limit=10, value="-1")["items"]) == 1


# ── #17 concurrent saves must not interleave into one temp file ──────────────

def test_issue17_config_save_uses_a_unique_temp_file(app_module, data_dir, monkeypatch):
    """A fixed .config.json.tmp let two processes publish a spliced file."""
    m = app_module
    monkeypatch.setattr(m, "DATA_DIR", data_dir)
    monkeypatch.setattr(m, "CONFIG_PATH", data_dir / "config.json")
    m.save_config({"rag": {"chunk_size": 180}})
    leftovers = list(data_dir.glob(".config.json.tmp"))
    assert not leftovers, "fixed temp filename still in use"
    assert (data_dir / "config.json").exists()
    assert json.loads((data_dir / "config.json").read_text())["rag"]["chunk_size"] == 180


def test_config_read_is_utf8_and_unreadable_is_not_quarantined(app_module, data_dir, monkeypatch):
    """A PermissionError is not corruption; renaming the file away destroyed it."""
    m = app_module
    path = data_dir / "config.json"
    path.write_text(json.dumps({"redis": {"host": "näs", "port": 6390}}), encoding="utf-8")
    monkeypatch.setattr(m, "CONFIG_PATH", path)
    os.chmod(path, 0o000)
    try:
        m.load_config()
        assert path.exists(), "unreadable config was quarantined"
        assert not list(data_dir.glob("*.corrupt-*"))
    finally:
        os.chmod(path, 0o644)
    path.write_text("{ not json", encoding="utf-8")
    m.load_config()
    assert list(data_dir.glob("*.corrupt-*")), "genuinely corrupt config was not quarantined"


# ── #18 the delete loop must terminate ───────────────────────────────────────

def test_issue18_delete_loop_is_bounded(app_module):
    """Without a cap, an index entry that outlives its key spins forever."""
    import inspect
    src = inspect.getsource(app_module.api_delete_document)
    assert "_MAX_DELETE_BATCHES" in src, "delete loop still has no iteration bound"


# ── retrieval: scoring and ordering ──────────────────────────────────────────

def test_keyword_only_hits_get_a_real_score(app_module):
    """BM25-only hits had no cosine and displayed as 0.0%, and the lexical
    exemption was unbounded so the whole BM25 tail reached the prompt."""
    m = app_module
    assert hasattr(m, "_LEXICAL_FLOOR_RATIO")
    assert 0 < m._LEXICAL_FLOOR_RATIO < 1


def test_cache_scope_ignores_disabled_instances(app_module):
    """An answer produced with an instance switched off must not replay once it
    is switched back on."""
    m = app_module

    async def fake(name):
        return ({"enabled": name != "archive"}, "default")

    original = m._rag_meta_cached_async
    m._rag_meta_cached_async = fake
    try:
        eff = asyncio.run(m._effective_rag_instances(["docs", "archive"]))
        assert eff == ["docs"]
        assert m._cache_scope(["docs", "archive"]) != m._cache_scope(eff)
    finally:
        m._rag_meta_cached_async = original


def test_cache_scope_is_order_independent(app_module):
    """Instance order is not part of the corpus identity."""
    m = app_module
    assert m._cache_scope(["a", "b"]) == m._cache_scope(["b", "a"])


def test_source_filter_escapes_tag_metacharacters(app_module):
    """An unescaped '|' turned one filter into an OR across every chunk."""
    m = app_module
    expr = m._source_infix_filter("a|b*c?d")
    assert expr is not None
    for ch in "|*?":
        assert f"\\{ch}" in expr, f"{ch!r} not escaped in {expr}"
    assert m._source_infix_filter("") is None


# ── #8 deleting one document must not take another's content ─────────────────

def test_issue8_dedup_is_scoped_per_source(app_module):
    """A global content hash stored an identical chunk once, so deleting the
    document ingested first also removed content the second one still needed."""
    import hashlib
    m = app_module
    same = "identical paragraph shared by two documents"
    norm = " ".join(same.lower().split())
    h_a = hashlib.sha256(f"a.pdf\x00{norm}".encode()).hexdigest()
    h_b = hashlib.sha256(f"b.pdf\x00{norm}".encode()).hexdigest()
    assert h_a != h_b, "identical text in different documents still collides"
    import inspect
    src = inspect.getsource(m.ingest_text)
    assert "source" in src[src.index("chunk_hashes = ["):src.index("chunk_hashes = [") + 220], \
        "dedup hash does not include the source"


# ── #11 a rebuild in progress is not a broken index ──────────────────────────

def test_issue11_backfill_is_not_reported_as_disabled(app_module):
    """Mid-rebuild num_docs is 0 while chunk keys exist; warning there also
    memoised the instance and suppressed the real diagnostic afterwards."""
    import inspect
    src = inspect.getsource(app_module._warn_if_index_empty_but_data_exists)
    assert "percent_indexed" in src


def test_empty_instance_is_memoised(app_module):
    """The clean result was never memoised, so an empty instance re-scanned the
    whole keyspace on every single query."""
    import inspect
    src = inspect.getsource(app_module._warn_if_index_empty_but_data_exists)
    head = src[:src.index("stored = ")] if "stored = " in src else src
    assert "_dim_mismatch_warned.add" in head, "the empty-instance path does not memoise"


# ── #13 a journalling failure must not fail a completed delete ───────────────

def test_issue13_log_failure_does_not_fail_the_delete(app_module):
    """The chunks were already gone, but append_log raising inside the try
    surfaced to the user as 'Delete failed'."""
    import inspect
    src = inspect.getsource(app_module.api_delete_document)
    tail = src[src.index("append_log("):]
    assert "except" in tail, "append_log is still unguarded"


# ── #16 the v3 schema must not index fields nothing reads ────────────────────

def test_issue16_unused_fields_are_not_indexed(app_module):
    """doc_id and pos forced a full DROPINDEX + backfill but no query uses them."""
    import inspect
    src = inspect.getsource(app_module._get_rag_index)
    fields = src[src.index("fields"):] if "fields" in src else src
    assert '"name": "doc_id"' not in fields
    assert '"name": "pos"' not in fields
    # …but they are still written to the hash for provenance.
    whole = open(app_module.__file__, encoding="utf-8").read()
    assert '"doc_id":' in whole and '"pos":' in whole


# ── frontend invariants (source-level; DOM behaviour verified separately) ────

def _html(app_module):
    return open(app_module.__file__.replace("main.py", "index.html"), encoding="utf-8").read()


def test_issue10_pending_regen_cleared_on_every_exit(app_module):
    """finalizeStreamingMsg returned before clearing _pendingRegen, so a stale
    version history was grafted onto an unrelated answer."""
    html = _html(app_module)
    fn = html[html.index("function finalizeStreamingMsg("):][:600]
    assert "_pendingRegen=null; return;" in fn.replace(" ", " "), \
        "the early return still leaves _pendingRegen set"
    for owner in ("function switchSession(", "function newSession("):
        body = html[html.index(owner):][:400]
        assert "_pendingRegen=null" in body, f"{owner} does not clear _pendingRegen"


def test_issue14_source_scope_is_per_conversation(app_module):
    """S.sourceFilter was global and sticky: set in conversation A it silently
    applied to B."""
    html = _html(app_module)
    for owner in ("function switchSession(", "function newSession("):
        body = html[html.index(owner):][:400]
        assert "setSourceScope('')" in body, f"{owner} does not reset the scope chip"


def test_issue15_modal_title_is_not_double_escaped(app_module):
    """showModal assigns the title via textContent, so pre-escaping rendered an
    instance called A&B as A&amp;B."""
    html = _html(app_module)
    line = [l for l in html.splitlines() if "Documents in" in l][0]
    assert "escHtml(r.name)" not in line


def test_cache_hit_records_the_turn(app_module):
    """A cached answer skipped sess.messages, breaking restore, regenerate, the
    version switcher and feedback at once."""
    html = _html(app_module)
    branch = html[html.index("case 'cache_hit':"):][:900]
    assert "sess.messages.push" in branch
    import inspect
    src = inspect.getsource(app_module.api_chat)
    hit = src[src.index("if hit:"):][:900]
    assert "save_session" in hit, "/api/chat still returns without persisting"


def test_no_kb_badge_is_gated_on_rag_used(app_module):
    """An empty chunk list also means 'RAG was switched off'; warning there sent
    the user to tune an irrelevant threshold."""
    html = _html(app_module)
    fn = html[html.index("function updateRagContext("):][:900]
    assert "ragUsed===false" in fn.replace(" ", "")


def test_crawl_can_be_paused_and_resumed(app_module):
    """Ingestion could only be cancelled, which discarded the queue and the
    visited set."""
    m = app_module
    assert hasattr(m, "_crawl_gates")
    assert any(getattr(r, "path", "") == "/api/crawl/pause" for r in m.app.routes)


# ── embedding model registry (multilingual default + mixed-corpus groundwork) ─

def test_registry_ids_are_a_stable_contract(app_module):
    """Chunks store the integer id, so renumbering or reusing one silently
    re-labels every vector already in Redis."""
    m = app_module
    expected = {
        0: "all-MiniLM-L6-v2",
        1: "intfloat/multilingual-e5-small",
        2: "intfloat/multilingual-e5-base",
        3: "BAAI/bge-m3",
    }
    for mid, repo in expected.items():
        assert m.EMBEDDING_MODELS[mid]["repo"] == repo, f"id {mid} was renumbered"
    assert len({s["repo"] for s in m.EMBEDDING_MODELS.values()}) == len(m.EMBEDDING_MODELS)


def test_default_model_is_multilingual(app_module):
    """The legacy default tokenised Thai and Chinese almost entirely as [UNK],
    so unrelated documents embedded to the same vector."""
    m = app_module
    assert m.DEFAULT_CONFIG["embedding"]["model"] == "intfloat/multilingual-e5-small"
    assert m.EMBEDDING_MODELS[m.DEFAULT_EMBEDDING_ID]["multilingual"] is True


def test_vector_field_is_keyed_by_width_not_model(app_module):
    """Vectors of different widths cannot share a RediSearch field, so a mixed
    index needs one field per dimension — two 384d models must share one."""
    m = app_module
    assert m.vector_field_for("intfloat/multilingual-e5-small") == \
           m.vector_field_for("all-MiniLM-L6-v2")
    assert m.vector_field_for("intfloat/multilingual-e5-base") != \
           m.vector_field_for("intfloat/multilingual-e5-small")
    assert m.vector_field_for("BAAI/bge-m3").endswith("1024")


def test_unknown_model_does_not_crash_the_registry(app_module):
    """A hand-edited config naming an unlisted model must degrade, not raise."""
    m = app_module
    assert m.embedding_id_for("some/unlisted-model") == -1
    spec = m.embedding_spec("some/unlisted-model")
    assert spec["query_prefix"] == "" and spec["passage_prefix"] == ""


def test_e5_prefixes_are_asymmetric_and_applied(app_module):
    """E5 needs 'query: ' on search text and 'passage: ' on stored text. Omitting
    them does not error — it quietly degrades retrieval."""
    m = app_module
    spec = m.EMBEDDING_MODELS[1]
    assert spec["query_prefix"] == "query: "
    assert spec["passage_prefix"] == "passage: "
    import inspect
    assert "is_query" in inspect.signature(m.embed).parameters
    assert "passage_prefix" in inspect.getsource(m.embed_batch)


def test_search_embeds_the_query_as_a_query(app_module):
    """search_rag must not embed the question as a passage."""
    import inspect
    src = inspect.getsource(app_module.search_rag)
    assert "is_query=True" in src


def test_chunks_record_their_embedding_model(app_module):
    """Without a per-chunk model id a corpus can never hold more than one."""
    whole = open(app_module.__file__, encoding="utf-8").read()
    assert '"emb_model":   str(embedding_id_for())' in whole, "chunks do not store the model id"
    idx_src = __import__("inspect").getsource(app_module._get_rag_index)
    assert '"name": "emb_model"' in idx_src, "emb_model is not indexed"


def test_schema_version_moved_for_the_model_change(app_module):
    """Existing indexes hold vectors from the old model; they must be rebuilt."""
    assert app_module._RAG_SCHEMA_VERSION >= 4


def test_same_width_model_swap_is_detected(app_module):
    """MiniLM and e5-small are both 384d, so a stale index stays structurally
    valid and Redis raises nothing — queries return confident nonsense. The
    width-based detector cannot see this; a model-id comparison must."""
    m = app_module
    assert m.EMBEDDING_MODELS[0]["dims"] == m.EMBEDDING_MODELS[1]["dims"] == 384
    assert hasattr(m, "warn_if_vectors_are_from_another_model")
    import inspect
    assert "warn_if_vectors_are_from_another_model" in \
        inspect.getsource(m._warn_if_index_empty_but_data_exists), "the warner is not wired in"


def test_embedding_change_guard_fires_on_name_not_dimension(app_module):
    """A same-width swap changes no dimension, so the guard must key on the
    model name or the switch goes completely undetected."""
    whole = open(app_module.__file__, encoding="utf-8").read()
    assert whole.count("if new_model and new_model != old_model:") >= 2, \
        "not every config path guards the model change"


def test_unrecorded_provenance_is_not_a_mismatch(app_module, monkeypatch):
    """Chunks written before emb_model existed report -1. Treating that as a
    different model fired an alarming, wrong warning on every existing install."""
    m = app_module
    calls = {}

    class FakeRC:
        def execute_command(self, *a):
            # one group: emb_model absent -> -1, 57805 chunks
            return [1, [b"emb_model", b"", b"n", b"57805"]]

    monkeypatch.setitem(m._config, "embedding", {"model": "all-MiniLM-L6-v2"})
    m._emb_mismatch_warned.discard("probe")
    m.warn_if_vectors_are_from_another_model("probe", FakeRC())
    assert "probe" not in m._emb_mismatch_warned, "unrecorded provenance warned as a mismatch"
