# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests, one per fixed defect.

Each test is named for the issue it locks down and states, in its docstring, the
behaviour that was wrong. When you fix a bug, add a test here that fails against
the old code and passes against the new one — that is the whole point of the
file. See tests/README.md.
"""
import asyncio
import inspect
import json
import os
import re
import time

from _jsrun import run_node

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────
# Every source-level assertion in this file goes through _code()/_js_fn(), and
# every one of them has a matching entry in tests/mutations.json that has been
# SHOWN to kill it (venv/bin/python3 tests/mutation_sweep.py). An assertion with
# no killing mutation is not coverage — see tests/README.md.

def _code(text: str) -> str:
    """Drop whole-line comments before matching against source.

    Issue #7 was 'guarded' by ``"casesensitive" in getsource(_get_rag_index)``.
    The real attribute is ``case_sensitive`` (underscore), which does not contain
    that substring — the only carrier was the prose comment explaining the fix.
    Deleting ``"case_sensitive": True`` from the schema therefore left the suite
    green (mutation M01). Same failure hit the gantt lane's ``useWidth`` (M46).
    """
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith(("#", "//")))


def _pysrc(obj) -> str:
    """Comment-stripped source of a Python object."""
    return _code(inspect.getsource(obj))


_JS_FN = re.compile(r"\n(?:async\s+)?function\s+[A-Za-z0-9_$]+\s*\(")


def _js_fn(html: str, header: str) -> str:
    """One JS function, sliced to the NEXT function — never a fixed width.

    switchSession() is 6,934 characters; the old assertions sliced 400 of them,
    so an early ``return`` inserted past that window (mutation M63) cleared
    neither _pendingRegen nor the source scope and the suite stayed green.
    """
    start = html.index(header)
    m = _JS_FN.search(html, start + 1)
    return _code(html[start:m.start() if m else len(html)])


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
    """Zero-overlap boundaries rose from 56% to 89% once the guard was added.

    The first version of this test was tautological: it measured overlap as
    ``set(a.split()[-32:]) & set(b.split()[:32])`` over a corpus whose every
    sentence read "sentence<i> about databases and storage systems", so the
    filler words alone made every boundary intersect. Measured 1.000 against a
    0.50 threshold WITH the overlap carry-back deleted (mutation M73). Overlap is
    now measured by shared SENTENCE identity, which the filler cannot fake:
    0.505 with the carry-back, 0.000 without it.
    """
    m = app_module
    text = " ".join(f"sentence{i} about databases and storage systems." for i in range(4000))
    chunks = m.chunk_text(text, size=180, overlap=32)

    def sentences(c):
        return {s.strip() for s in c.split(".") if s.strip()}

    joined = sum(1 for a, b in zip(chunks, chunks[1:]) if sentences(a) & sentences(b))
    ratio = joined / max(1, len(chunks) - 1)
    assert ratio >= 0.45, (
        f"only {ratio:.1%} of the {len(chunks)} chunk boundaries share a sentence "
        f"with their predecessor — the overlap carry-back is not running"
    )


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
    # Whole function, not the first 900 characters: newSession() is 1,044 chars,
    # so the old slice left the tail unchecked (mutation M58 put the call there).
    body = _js_fn(html, "function newSession(")
    assert "_rememberSessionId" not in body, (
        "newSession() still records the id; it must be recorded on first send"
    )


# ── #7 per-document delete must match the exact source ───────────────────────

def test_issue7_delete_is_case_sensitive(app_module, clean_redis, monkeypatch):
    """RediSearch TAG fields casefold, so deleting report.pdf also took Report.pdf.

    This used to hand-write its own FT.CREATE with CASESENSITIVE spelled out, so
    it tested RediSearch rather than RediRecall: deleting ``case_sensitive`` from
    the PRODUCTION schema (mutation M01) left it green. The index is now built by
    _get_rag_index() itself.
    """
    m = app_module
    rc = clean_redis
    inst = "t7"
    ns = rc.key("t7")                       # __rrtest_<pid>__t7 → conftest purges it
    monkeypatch.setattr(m, "rag_prefix", lambda i: ns)
    idx = m._get_rag_index(inst, rc)
    idx.create(overwrite=True)
    try:
        rc.hset(f"{ns}:chunk:1", mapping={"source": "report.pdf", "text": "lower"})
        rc.hset(f"{ns}:chunk:2", mapping={"source": "Report.pdf", "text": "upper"})
        time.sleep(0.4)
        # '.' is a TAG metacharacter; production escapes via _TAG_ESCAPE_RE.
        def search(name):
            esc = m._TAG_ESCAPE_RE.sub(r"\\\1", name)
            return rc.execute_command("FT.SEARCH", f"{ns}:idx", f"@source:{{{esc}}}",
                                      "RETURN", "1", "text")
        res = search("report.pdf")
        assert res[0] == 1, f"exact-case query matched {res[0]} docs, expected 1"
        assert res[2][1] == b"lower", f"matched the wrong document: {res[2]}"
        res_up = search("Report.pdf")
        assert res_up[0] == 1, f"upper-case query matched {res_up[0]} docs, expected 1"
        assert res_up[2][1] == b"upper", "the two cases resolve to the same doc"
    finally:
        try:
            rc.execute_command("FT.DROPINDEX", f"{ns}:idx")
        except Exception:
            pass


def test_issue7_schema_marks_source_casesensitive(app_module):
    """The live schema must carry case_sensitive, not just a comment saying so.

    Old assertion: ``"casesensitive" in getsource(_get_rag_index).lower()``. The
    attribute is spelled ``case_sensitive``, so the ONLY carrier was the comment
    above it — mutation M01 deleted the attribute and this stayed green. Assert
    the code form, with comments stripped.
    """
    src = _pysrc(app_module._get_rag_index)
    assert '"case_sensitive": True' in src, \
        "source TAG is case-folding again — a per-document delete will over-match"


# ── #9 feedback endpoint must be bounded ─────────────────────────────────────

def test_issue9_feedback_limit_cannot_be_bypassed(app_module):
    """items[-max(0, limit):] is items[0:] for limit<=0 — the whole store leaked."""
    m = app_module
    m._feedback = [{"i": i} for i in range(50)]
    for bad in (0, -5, -1):
        got = m.api_feedback_list(limit=bad)
        assert len(got["items"]) < 50, f"limit={bad} returned {len(got['items'])} of 50"
    assert len(m.api_feedback_list(limit=3)["items"]) == 3


def test_issue9_feedback_payload_is_size_capped(app_module, data_dir, monkeypatch):
    """An unbounded POST body let one request bloat the on-disk store.

    The old assertions only checked that _MAX_FEEDBACK_FIELD exists and is small.
    Deleting the slice that applies it (mutation M05) left both true. This posts
    an oversized field through the real handler and measures what was stored.
    """
    m = app_module
    monkeypatch.setattr(m, "FEEDBACK_PATH", data_dir / "feedback.json")
    monkeypatch.setattr(m, "_feedback", [])
    assert m._MAX_FEEDBACK_FIELD <= 20000, "the cap itself has been raised too far"
    asyncio.run(m.api_feedback({"comment": "x" * 50000, "answer": "y" * 50000, "value": 1}))
    stored = m._feedback[-1]
    assert len(stored["comment"]) == m._MAX_FEEDBACK_FIELD, \
        f"stored {len(stored['comment'])} chars of a 50,000-char comment; the cap is not applied"
    assert len(stored["answer"]) == m._MAX_FEEDBACK_FIELD, "the cap is applied to only one field"
    assert stored["value"] == 1, "non-string fields must pass through untouched"
    on_disk = json.loads((data_dir / "feedback.json").read_text())
    assert len(on_disk[-1]["comment"]) == m._MAX_FEEDBACK_FIELD, "the file kept the full body"


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
    """A fixed .config.json.tmp let two processes publish a spliced file.

    The old check globbed for a leftover .config.json.tmp. On the success path
    os.replace() consumes the temp file whatever it is named, so the glob was
    empty either way — reinstating the fixed name (mutation M02) left it green.
    Record the names actually used and require two saves to use two files.
    """
    m = app_module
    monkeypatch.setattr(m, "DATA_DIR", data_dir)
    monkeypatch.setattr(m, "CONFIG_PATH", data_dir / "config.json")

    names = []
    real_mkstemp = m.tempfile.mkstemp

    def recording_mkstemp(*a, **kw):
        fd, path = real_mkstemp(*a, **kw)
        names.append(os.path.basename(path))
        return fd, path

    monkeypatch.setattr(m.tempfile, "mkstemp", recording_mkstemp)
    m.save_config({"rag": {"chunk_size": 180}})
    m.save_config({"rag": {"chunk_size": 200}})

    assert len(names) == 2, f"save_config made {len(names)} temp files for 2 saves — {names}"
    assert names[0] != names[1], f"both saves used the same temp file {names[0]!r}"
    for n in names:
        assert n.startswith(".config.json.") and n.endswith(".tmp"), n
    assert not list(data_dir.glob(".config.json*.tmp")), "a temp file was left behind"
    assert json.loads((data_dir / "config.json").read_text())["rag"]["chunk_size"] == 200


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

class _FakeDeleteRedis:
    """Just enough of redis.Redis to drive api_delete_document, with a scripted
    FT.SEARCH. Asking for one page more than the script holds raises, so an
    unbounded loop fails fast instead of hanging the suite."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.searches = 0
        self.deleted = []
        self.srems = []

    def execute_command(self, *args):
        if args[0] == "FT.SEARCH":
            self.searches += 1
            if self.searches > len(self.pages):
                raise RuntimeError(
                    f"FT.SEARCH called {self.searches} times — the delete loop is unbounded")
            return self.pages[self.searches - 1]
        return None

    def pipeline(self, transaction=False):
        outer = self

        class _P:
            def delete(self, k):
                outer.deleted.append(k)

            def execute(self):
                return []
        return _P()

    def srem(self, *a):
        self.srems.append(a)
        return 0


def _full_page(n=200, tag=b"chunk"):
    """An FT.SEARCH reply holding a whole page — the shape that keeps the loop going."""
    items = []
    for i in range(n):
        items += [b"k%d" % i, [b"text", tag + b" %d" % i]]
    return [n] + items


def test_issue18_delete_loop_is_bounded(app_module, monkeypatch):
    """Without a cap, an index entry that outlives its key spins forever.

    The old assertion was ``"_MAX_DELETE_BATCHES" in getsource(...)`` — satisfied
    by ``while True:  # _MAX_DELETE_BATCHES`` (mutation M04). This drives the real
    handler against an index that never stops returning full pages.
    """
    m = app_module
    monkeypatch.setattr(m, "_MAX_DELETE_BATCHES", 3)
    # Four pages available, three permitted: a fourth FT.SEARCH raises.
    fake = _FakeDeleteRedis([_full_page() for _ in range(4)])
    monkeypatch.setattr(m, "_rc_for", lambda *a, **k: fake)
    monkeypatch.setattr(m, "append_log", lambda e: None)

    out = m.api_delete_document(instance="t18", source="ghost.pdf")
    assert out["ok"] is True, out
    assert fake.searches == 3, \
        f"the loop ran {fake.searches} batches with the cap at 3 — it is not bounded"
    assert out["deleted"] == 600, out


# ── retrieval: scoring and ordering ──────────────────────────────────────────

def test_keyword_only_hits_get_a_real_score(app_module):
    """BM25-only hits had no cosine and displayed as 0.0%, and the lexical
    exemption was unbounded so the whole BM25 tail reached the prompt.

    The old assertions only checked that _LEXICAL_FLOOR_RATIO exists and is
    between 0 and 1; setting ``lex_floor = 0.0`` at the one place it is applied
    (mutation M06) left both true. This EXECUTES the production selection
    expression — lifted verbatim out of search_rag — against synthetic hits.
    """
    import textwrap
    m = app_module
    assert 0 < m._LEXICAL_FLOOR_RATIO < 1

    src = _pysrc(m.search_rag)
    assert "lex_floor = threshold * _LEXICAL_FLOOR_RATIO" in src, \
        "the lexical floor is no longer derived from the cosine threshold"
    start = src.index("lex_floor = threshold *")
    end = src.index("for c in kept:", start)
    snippet = textwrap.dedent(src[src.rindex("\n", 0, start) + 1:end])

    threshold = 0.50
    raw_results = [
        {"id": "vec_hit",      "score": 0.60, "lexical": False},   # above threshold
        {"id": "vec_miss",     "score": 0.20, "lexical": False},   # below, not lexical
        {"id": "lex_near",     "score": 0.40, "lexical": True},    # 0.8 × threshold
        {"id": "lex_tail",     "score": 0.10, "lexical": True},    # 0.2 × threshold
        {"id": "lex_zero",     "score": 0.00, "lexical": True},    # the BM25 tail
    ]
    ns = {"threshold": threshold, "raw_results": raw_results,
          "_LEXICAL_FLOOR_RATIO": m._LEXICAL_FLOOR_RATIO}
    exec(compile(snippet, "<search_rag lexical floor>", "exec"), ns)
    kept = {c["id"] for c in ns["kept"]}

    assert "vec_hit" in kept, "a hit above the cosine threshold was dropped"
    assert "lex_near" in kept, "a keyword hit just under the threshold was dropped"
    assert "vec_miss" not in kept, "the cosine threshold is not being applied"
    assert "lex_tail" not in kept and "lex_zero" not in kept, (
        f"the BM25 tail reached the prompt: kept={sorted(kept)} — the lexical "
        f"exemption is unbounded again"
    )


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
    m = app_module
    # The old first half computed two sha256 digests inside the test and asserted
    # they differed — a property of sha256, not of RediRecall, and unfailable. The
    # old second half looked for the word "source" in a 220-char window, which is
    # common English. Assert the production expression instead: the hash input must
    # start with the source, so the same paragraph in two documents hashes twice.
    src = _pysrc(m.ingest_text)
    expr = re.search(r"hashlib\.sha256\(f?\"[^\"]*\"\.encode\(\)\)\.hexdigest\(\)", src)
    assert expr, "the dedup hash expression moved; update this test"
    assert "{source}" in expr.group(0), \
        f"dedup hash is global again, not per-source: {expr.group(0)}"


# ── #11 a rebuild in progress is not a broken index ──────────────────────────

class _FakeInfoRC:
    """FT.INFO / FT.AGGREGATE / SCAN for _warn_if_index_empty_but_data_exists."""

    def __init__(self, num_docs=b"0", percent_indexed=b"1", keys=()):
        self._info = [b"num_docs", num_docs, b"percent_indexed", percent_indexed]
        self._keys = list(keys)
        self.scans = 0

    def execute_command(self, *a):
        if a[0] == "FT.INFO":
            return self._info
        return [1, [b"emb_model", b"", b"n", b"0"]]      # the model-provenance probe

    def scan_iter(self, pattern, count=200):
        self.scans += 1
        return iter(list(self._keys))


def test_issue11_backfill_is_not_reported_as_disabled(app_module, monkeypatch):
    """Mid-rebuild num_docs is 0 while chunk keys exist; warning there also
    memoised the instance and suppressed the real diagnostic afterwards."""
    m = app_module
    monkeypatch.setattr(m, "_dim_mismatch_warned", set())
    assert "percent_indexed" in _pysrc(m._warn_if_index_empty_but_data_exists)

    rebuilding = _FakeInfoRC(num_docs=b"0", percent_indexed=b"0.42",
                             keys=[b"rag:x:chunk:1", b"rag:x:chunk:2"])
    m._warn_if_index_empty_but_data_exists("rebuilding", rebuilding)
    assert "rebuilding" not in m._dim_mismatch_warned, \
        "a rebuild in progress was memoised, which suppresses the real diagnostic later"
    assert rebuilding.scans == 0, "it scanned the keyspace during a rebuild"

    # A genuinely broken instance — indexing finished, 0 indexed, keys present.
    broken = _FakeInfoRC(num_docs=b"0", percent_indexed=b"1",
                         keys=[b"rag:x:chunk:1", b"rag:x:chunk:2"])
    m._warn_if_index_empty_but_data_exists("broken", broken)
    assert "broken" in m._dim_mismatch_warned, "the real 'RAG disabled' case was not reported"


def test_empty_instance_is_memoised(app_module, monkeypatch):
    """The clean result was never memoised, so an empty instance re-scanned the
    whole keyspace on every single query.

    The old check sliced the source at ``stored = `` and silently fell back to the
    WHOLE function when that literal disappeared, so the assertion could drift onto
    the other memoisation call. Count the scans instead.
    """
    m = app_module
    monkeypatch.setattr(m, "_dim_mismatch_warned", set())
    rc = _FakeInfoRC(num_docs=b"0", percent_indexed=b"1", keys=[])
    for _ in range(3):
        m._warn_if_index_empty_but_data_exists("empty", rc)
    assert rc.scans == 1, \
        f"an empty instance re-scanned the keyspace {rc.scans} times over 3 queries"
    assert "empty" in m._dim_mismatch_warned, "the empty-instance path does not memoise"


# ── #13 a journalling failure must not fail a completed delete ───────────────

def test_issue13_log_failure_does_not_fail_the_delete(app_module, monkeypatch):
    """The chunks were already gone, but append_log raising inside the try
    surfaced to the user as 'Delete failed'.

    The old check sliced from ``append_log(`` to the end of the function and
    asserted ``"except" in tail`` — the tail also contains the handler's OUTER
    ``except Exception as e:`` (measured: 2 in the slice), so removing the inner
    guard entirely (mutation M03) left it green. This makes append_log raise.
    """
    m = app_module
    page = [1, b"k0", [b"text", b"the only chunk"]]
    fake = _FakeDeleteRedis([page])
    monkeypatch.setattr(m, "_rc_for", lambda *a, **k: fake)

    def boom(entry):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m, "append_log", boom)

    out = m.api_delete_document(instance="t13", source="report.pdf")
    assert out == {"ok": True, "source": "report.pdf", "deleted": 1}, out
    assert fake.deleted == ["k0"], "the chunk was not actually deleted"
    assert fake.srems, "the dedup hashes were not released"


# ── #16 the v3 schema must not index fields nothing reads ────────────────────

def test_issue16_unused_fields_are_not_indexed(app_module):
    """doc_id and pos forced a full DROPINDEX + backfill but no query uses them."""
    src = _pysrc(app_module._get_rag_index)          # comment-stripped: a commented
    fields = src[src.index("fields"):] if "fields" in src else src   # {"name":"doc_id"}
    assert '"name": "doc_id"' not in fields, "doc_id is indexed again"   # cannot false-red
    assert '"name": "pos"' not in fields, "pos is indexed again"
    # …but they are still WRITTEN to the hash for provenance, by add_chunks itself.
    # The old whole-file check survived probe P05 (dropping the write from add_chunks)
    # because api_rag_documents writes a SECOND doc_id 5,700 lines away — scope it.
    writer = _pysrc(app_module.add_chunks)
    assert '"doc_id":' in writer, "add_chunks no longer writes doc_id provenance"
    assert '"pos":' in writer, "add_chunks no longer writes pos provenance"


# ── frontend invariants (source-level; DOM behaviour verified separately) ────

def _html(app_module):
    return open(app_module.__file__.replace("main.py", "index.html"), encoding="utf-8").read()


def _returns(body: str) -> list[str]:
    """Every `return` statement in a JS function body, with its own statement text."""
    out = []
    for m in re.finditer(r"\breturn\b[^;}\n]*", body):
        line = body[body.rfind("\n", 0, m.start()) + 1:body.find("\n", m.start())]
        out.append(line.strip())
    return out


def test_issue10_pending_regen_cleared_on_every_exit(app_module):
    """finalizeStreamingMsg returned before clearing _pendingRegen, so a stale
    version history was grafted onto an unrelated answer.

    The old test sliced a fixed 600 chars of a 2,065-char function and 400 chars
    of a 6,934-char one; an early exit added past the window (mutation M63) left
    _pendingRegen set with the suite green. Slice to the next function, and check
    EVERY return in the two functions that own the flag.
    """
    html = _html(app_module)
    fin = _js_fn(html, "function finalizeStreamingMsg(")
    assert "_pendingRegen=null; return;" in fin, \
        "the early return still leaves _pendingRegen set"
    for owner in ("function switchSession(", "function newSession("):
        body = _js_fn(html, owner)
        assert "_pendingRegen=null" in body, f"{owner} does not clear _pendingRegen"
        # An exit that runs BEFORE the reset is the exact shape of the bug.
        head = body[:body.index("_pendingRegen=null")]
        assert not _returns(head), (
            f"{owner} can return before clearing _pendingRegen: {_returns(head)}"
        )


def test_issue14_source_scope_is_per_conversation(app_module):
    """S.sourceFilter was global and sticky: set in conversation A it silently
    applied to B."""
    html = _html(app_module)
    for owner in ("function switchSession(", "function newSession("):
        body = _js_fn(html, owner)
        assert "setSourceScope('')" in body, f"{owner} does not reset the scope chip"
        head = body[:body.index("setSourceScope('')")]
        assert not _returns(head), (
            f"{owner} can return before resetting the scope chip: {_returns(head)}"
        )


def test_issue15_modal_title_is_not_double_escaped(app_module):
    """showModal assigns the title via textContent, so pre-escaping rendered an
    instance called A&B as A&amp;B.

    This forbids escaping, which is only correct while the premise holds. Switching
    showModal to innerHTML (mutation M59) turns the title into an XSS sink that
    NEEDS escaping — and this test would then actively block the fix. Assert the
    premise first; if it ever changes, both halves change together.
    """
    html = _html(app_module)
    fn = html[html.index("function showModal("):html.index("function closeModal(")]
    assert "modal-title').textContent=title" in fn, (
        "showModal no longer assigns the title as TEXT — pre-escaping is now "
        "required, not forbidden, and every caller below must be revisited"
    )
    assert "innerHTML=title" not in fn, "the modal title is an HTML sink"
    line = [l for l in html.splitlines() if "Documents in" in l][0]
    assert "escHtml(r.name)" not in line


def test_cache_hit_records_the_turn(app_module, monkeypatch):
    """A cached answer skipped sess.messages, breaking restore, regenerate, the
    version switcher and feedback at once.

    The Python half DRIVES /api/chat down the cache-hit branch and checks the turn
    was persisted, rather than grepping api_chat's source for the word 'save_session'
    (which survived probe P06 — wrapping the call in `if False:` keeps the word).
    The JS half slices the cache_hit case to the NEXT case, not a fixed 900 chars.
    """
    html = _html(app_module)
    s = html.index("case 'cache_hit':")
    branch = _code(html[s:html.index("case '", s + 10)])
    assert "sess.messages.push" in branch, "the WS cache_hit path does not record the turn"

    m = app_module
    saved = []
    monkeypatch.setattr(m, "save_session", lambda sid, msgs: saved.append((sid, list(msgs))))
    monkeypatch.setattr(m, "cache_lookup",
                        lambda q, thr, scope: {"response": "cached answer", "score": 0.99, "chunks": []})
    monkeypatch.setattr(m, "_cache_scope", lambda *a, **k: "scope")
    async def _eff(x):
        return list(x)
    monkeypatch.setattr(m, "_effective_rag_instances", _eff)
    try:
        out = asyncio.run(m.api_chat({"content": "q", "session_id": "probe-sid",
                                      "provider": "ollama", "model": "probe-model",
                                      "use_cache": True, "rag_instances": []}))
        assert out["cache_hit"] is True, "the cache hit was not taken"
        assert saved, "a cached answer was returned without persisting the turn"
        assert [x["role"] for x in saved[-1][1][-2:]] == ["user", "assistant"], \
            f"the persisted turn is not a user/assistant pair: {saved[-1][1][-2:]}"
        assert saved[-1][1][-1]["content"] == "cached answer", \
            "the assistant message persisted is not the cached answer"
    finally:
        m._sessions.pop("probe-sid", None)


def test_no_kb_badge_is_gated_on_rag_used(app_module):
    """An empty chunk list also means 'RAG was switched off'; warning there sent
    the user to tune an irrelevant threshold.

    Slices the WHOLE function (to the next function, comment-stripped) rather than a
    fixed 900 chars: inverting the guard to `ragUsed===true` behind a `// was:
    ragUsed===false` comment (probe P04) kept the substring in the raw window, so the
    old test stayed green. _js_fn strips comments, so the decoy no longer counts.
    """
    html = _html(app_module)
    fn = _js_fn(html, "function updateRagContext(")
    assert "ragUsed===false" in fn.replace(" ", ""), \
        "the no-KB badge is no longer gated on ragUsed===false"


def test_crawl_can_be_paused_and_resumed(app_module):
    """Ingestion could only be cancelled, which discarded the queue and the
    visited set. Pause/resume toggles a gate the worker awaits between pages.

    Drives api_crawl_pause instead of asserting the gate dict merely EXISTS: probe
    P08 replaced `gate.clear()` with `pass`, so pause returned ok while the gate
    stayed set and the worker never blocked — a defect hasattr() cannot see. (The old
    test's only killer was M62, a bare rename of _crawl_gates, which broke no
    behaviour — it "failed" this test only by making hasattr() false.)
    """
    m = app_module

    async def drive():
        gate = asyncio.Event()
        gate.set()
        m._crawl_gates["http://probe"] = gate
        try:
            await m.api_crawl_pause({"url": "http://probe", "paused": True})
            assert not gate.is_set(), "pause returned ok but never cleared the gate"
            await m.api_crawl_pause({"url": "http://probe", "paused": False})
            assert gate.is_set(), "resume returned ok but never set the gate"
        finally:
            m._crawl_gates.pop("http://probe", None)

    asyncio.run(drive())
    # …and the handler must be REACHABLE: the drive calls the function directly, so
    # it cannot see the POST route being renamed/removed (mutation M50). The button
    # posts to this path; if it is gone the pause 404s no matter how good the handler.
    assert any(getattr(rt, "path", "") == "/api/crawl/pause" for rt in m.app.routes), \
        "the POST /api/crawl/pause route is gone — the pause button 404s"


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


def test_e5_prefixes_are_asymmetric_and_applied(app_module, cfg, monkeypatch):
    """E5 needs 'query: ' on search text and 'passage: ' on stored text. Omitting
    them does not error — it quietly degrades retrieval.

    The old assertions were: the registry holds the two prefixes, ``embed`` has an
    ``is_query`` parameter, and the string "passage_prefix" appears in
    ``embed_batch``. Dropping the prefixes at the one place they are applied
    (mutation M12: ``prefix = ""``) left all three true. This records the strings
    the model actually receives.
    """
    import numpy as np
    m = app_module
    spec = m.EMBEDDING_MODELS[1]
    assert spec["query_prefix"] == "query: "
    assert spec["passage_prefix"] == "passage: "

    seen = []

    class _StubModel:
        def encode(self, texts, **kw):
            seen.append(texts)
            n = 1 if isinstance(texts, str) else len(texts)
            return np.zeros(384 if n == 1 else (n, 384), dtype=np.float32)

        def get_sentence_embedding_dimension(self):
            return 384

    monkeypatch.setitem(m._config, "embedding", {"model": "intfloat/multilingual-e5-small"})
    monkeypatch.setattr(m, "get_embed_model", lambda: _StubModel())

    m.embed("how do i reindex?", is_query=True)
    m.embed("the index is rebuilt on schema change")
    m.embed_batch(["chunk one", "chunk two"])

    assert seen[0] == "query: how do i reindex?", f"query got {seen[0]!r}"
    assert seen[1] == "passage: the index is rebuilt on schema change", f"passage got {seen[1]!r}"
    assert seen[2] == ["passage: chunk one", "passage: chunk two"], f"batch got {seen[2]!r}"

    # A model with no prefixes must not gain one.
    monkeypatch.setitem(m._config, "embedding", {"model": "all-MiniLM-L6-v2"})
    m.embed("plain", is_query=True)
    assert seen[3] == "plain", f"a non-E5 model was given a prefix: {seen[3]!r}"


def test_search_embeds_the_query_as_a_query(app_module):
    """search_rag must not embed the question as a passage.

    ``"is_query=True" in getsource(search_rag)`` was satisfied by either of the
    function's TWO call sites (the KNN leg and the index-missing retry), so
    breaking one (mutation M08) left it green. Census every call instead: EVERY
    place search_rag embeds the question must ask for the query prefix.
    """
    import numpy as np
    m = app_module

    # Behavioural half: the first thing search_rag does with the question is embed
    # it, before any Redis call, so a fake client is enough to reach that line.
    recorded = []
    real_embed = m.embed

    def spy(text, is_query=False):
        recorded.append((text, is_query))
        return np.zeros(384, dtype=np.float32)

    m.embed = spy
    try:
        try:
            m.search_rag("t8", "how do i reindex?", rc=object())
        except Exception:
            pass          # the fake client fails later; we only need the embed call
    finally:
        m.embed = real_embed
    assert recorded, "search_rag never embedded the question"
    assert recorded[0] == ("how do i reindex?", True), \
        f"the question was embedded as {recorded[0]!r} — a passage, not a query"

    # Census half: the KNN leg and the index-missing retry each embed the query, and
    # both must embed it as a QUERY. Pinned to EXACTLY two (measured): `>= 2` could
    # not tell two well-formed sites from a third that slipped in embedding a passage.
    sites = re.findall(r"embed\(\s*query\s*(?:,[^)]*)?\)", _pysrc(m.search_rag))
    assert len(sites) == 2, (
        f"expected exactly the KNN leg and the index-missing retry to embed the "
        f"query; found {len(sites)}: {sites}"
    )
    bad = [s for s in sites if "is_query=True" not in s]
    assert not bad, f"search_rag embeds the question as a PASSAGE at {len(bad)} site(s): {bad}"


def test_chunks_record_their_embedding_model(app_module):
    """Without a per-chunk model id a corpus can never hold more than one."""
    whole = _code(open(app_module.__file__, encoding="utf-8").read())
    # Whitespace-insensitive: the literal used to be pinned with its exact column
    # alignment, so a reformat would have false-red a correct file.
    assert re.search(r'"emb_model"\s*:\s*str\(embedding_id_for\(\)\)', whole), \
        "chunks do not store the model id"
    assert '"name": "emb_model"' in _pysrc(app_module._get_rag_index), "emb_model is not indexed"


def test_schema_version_moved_for_the_model_change(app_module):
    """Existing indexes hold vectors from the old model; they must be rebuilt.

    ``>= 4`` was a rubber stamp: the value is 5, and reverting it to 4 (mutation
    M56) skips _migrate_vector_field on every existing install — the whole corpus
    stays indexed under the old field name and goes dark with no error. Pin the
    exact value; bump it here in the SAME commit as any _get_rag_index schema edit.
    """
    m = app_module
    assert m._RAG_SCHEMA_VERSION == 5, (
        f"_RAG_SCHEMA_VERSION is {m._RAG_SCHEMA_VERSION}; the v5 vector-field rename "
        f"needs it at 5 or ensure_rag_index skips the migration"
    )
    # …and the migration must still be gated on it.
    assert "if have_ver < 5:" in _pysrc(m._ensure_rag_index_locked), \
        "the v5 vector-field migration is no longer reached"


class _FakeAggRC:
    """A client whose FT.AGGREGATE returns one emb_model group."""

    def __init__(self, emb_model: bytes, n: bytes = b"57805"):
        self._row = [b"emb_model", emb_model, b"n", n]

    def execute_command(self, *a):
        return [1, self._row]


def test_same_width_model_swap_is_detected(app_module, monkeypatch):
    """MiniLM and e5-small are both 384d, so a stale index stays structurally
    valid and Redis raises nothing — queries return confident nonsense. The
    width-based detector cannot see this; a model-id comparison must.

    The old assertions were ``hasattr(m, "warn_if_vectors_are_from_another_model")``
    plus the NAME appearing in the caller's source. Replacing the whole function
    body with ``return`` (mutation M14) left both true. This calls it.
    """
    m = app_module
    assert m.EMBEDDING_MODELS[0]["dims"] == m.EMBEDDING_MODELS[1]["dims"] == 384
    # …and it must still be wired into the query path.
    assert "warn_if_vectors_are_from_another_model" in \
        _pysrc(m._warn_if_index_empty_but_data_exists), "the warner is not wired in"

    # Active model = e5-small (registry id 1); the corpus holds MiniLM (id 0)
    # vectors. Same 384 width, so nothing else in the stack can notice.
    monkeypatch.setitem(m._config, "embedding", {"model": "intfloat/multilingual-e5-small"})
    m._emb_mismatch_warned.discard("swap")
    try:
        m.warn_if_vectors_are_from_another_model("swap", _FakeAggRC(b"0"))
        assert "swap" in m._emb_mismatch_warned, \
            "a same-width model swap was not detected — queries return confident nonsense"
        # Memoised: a second call must not re-warn.
        before = set(m._emb_mismatch_warned)
        m.warn_if_vectors_are_from_another_model("swap", _FakeAggRC(b"0"))
        assert set(m._emb_mismatch_warned) == before
    finally:
        m._emb_mismatch_warned.discard("swap")

    # The matching model must NOT warn.
    m._emb_mismatch_warned.discard("same")
    m.warn_if_vectors_are_from_another_model("same", _FakeAggRC(b"1"))
    assert "same" not in m._emb_mismatch_warned, "the active model was reported as a mismatch"


def test_embedding_change_guard_fires_on_name_not_dimension(app_module, cfg, monkeypatch):
    """A same-width swap changes no dimension, so the guard must key on the
    model name or the switch goes completely undetected.

    The old assertion counted an exact source LINE twice — a formatting check
    that would false-red the correct refactor (extracting the guard into one
    helper) while a gutted guard body kept the line intact. Drive the handler.
    """
    m = app_module
    monkeypatch.setattr(m, "save_config", lambda c: None)
    monkeypatch.setattr(m, "invalidate_redis_clients", lambda: None)
    monkeypatch.setattr(m, "invalidate_provider_clients", lambda: None)
    monkeypatch.setattr(m, "list_rag_instances", lambda: [{"name": "docs", "chunks": 12}])
    resets = []

    async def _reset():
        resets.append(1)

    monkeypatch.setattr(m, "_reset_index_markers_for_embedding_change", _reset)
    # MiniLM → e5-small: both 384d, so nothing about the DIMENSION changes.
    m._config["embedding"] = {"model": "all-MiniLM-L6-v2"}

    with pytest.raises(m.HTTPException) as ei:
        asyncio.run(m.api_save_config({"embedding": {"model": "intfloat/multilingual-e5-small"}}))
    assert ei.value.status_code == 409, f"same-width model swap returned {ei.value.status_code}"
    assert not resets, "the markers were reset despite refusing the change"

    # Forced: allowed, but every index marker must be reset so it rebuilds.
    asyncio.run(m.api_save_config({"embedding": {"model": "intfloat/multilingual-e5-small"},
                                   "force_embedding_change": True}))
    assert resets == [1], "a forced model change did not reset the index schema markers"

    # No model change: no reset, no refusal.
    asyncio.run(m.api_save_config({"embedding": {"model": "intfloat/multilingual-e5-small"}}))
    assert resets == [1], "an unchanged model still reset the index markers"

    # The config-IMPORT path carries the same consequence and must run it too.
    assert "_reset_index_markers_for_embedding_change" in _pysrc(m.api_import_config), \
        "importing a config that changes the embedding model skips the rebuild"


def test_unrecorded_provenance_is_not_a_mismatch(app_module, monkeypatch):
    """Chunks written before emb_model existed report -1. Treating that as a
    different model fired an alarming, wrong warning on every existing install."""
    m = app_module
    monkeypatch.setitem(m._config, "embedding", {"model": "all-MiniLM-L6-v2"})
    # one group: emb_model absent -> -1, 57805 chunks
    m._emb_mismatch_warned.discard("probe")
    m.warn_if_vectors_are_from_another_model("probe", _FakeAggRC(b""))
    assert "probe" not in m._emb_mismatch_warned, "unrecorded provenance warned as a mismatch"

    # The negative case alone is satisfied by a function that does nothing at all
    # (mutation M14 replaced the body with `return` and this stayed green), so
    # assert the positive case in the same breath: a REAL other model must warn.
    m._emb_mismatch_warned.discard("probe2")
    try:
        m.warn_if_vectors_are_from_another_model("probe2", _FakeAggRC(b"1"))
        assert "probe2" in m._emb_mismatch_warned, \
            "a genuinely different model id did not warn — the detector is a no-op"
    finally:
        m._emb_mismatch_warned.discard("probe2")


# ── the console script must handle arguments, not start a server ─────────────

def test_cli_help_exits_instead_of_starting_a_server():
    """cli() ignored sys.argv entirely and went straight to uvicorn.run(), so
    `redirecall --help` started a server and hung until CI's 25-minute job
    timeout killed it. --port was silently discarded for the same reason."""
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.argv=['redirecall','--help'];"
                        "import redirecall.main as m; m.cli()"],
                       cwd=root, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"--help exited {r.returncode}"
    assert "usage: redirecall" in r.stdout
    assert "--port" in r.stdout and "--host" in r.stdout


def test_cli_rejects_unknown_arguments():
    """An unrecognised flag must fail loudly rather than be discarded and the
    server started anyway."""
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.argv=['redirecall','--bogus'];"
                        "import redirecall.main as m; m.cli()"],
                       cwd=root, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0
    assert "unrecognized arguments" in r.stderr


# ── #29 the vector field must be the one the helper advertises ───────────────

@pytest.mark.parametrize("model,dims,expected", [
    ("intfloat/multilingual-e5-small", 384, "embedding_384"),
    ("BAAI/bge-m3", 1024, "embedding_1024"),
])
def test_issue29_vector_field_matches_the_helper(app_module, monkeypatch, model, dims, expected):
    """vector_field_for() returned embedding_384 while the schema used the literal
    'embedding', so /api/embedding/models reported a field that did not exist and a
    mixed-model index had nowhere to put a second width.

    Builds the SearchIndex schema (offline, redis_client=None) and reads the vector
    field name back. Under TWO model configs it must equal vector_field_for(), so no
    single literal — 'embedding' (M37) or 'embedding_384' (probe P03) — satisfies
    both widths. The old test grepped getsource, which a decoy `vector_field_for` in
    a comment defeats; get_embed_model is stubbed so the width comes from config, not
    a real model load (0 ms vs ~2.2 s).
    """
    import types
    m = app_module
    monkeypatch.setitem(m._config, "embedding", {"model": model})
    monkeypatch.setattr(m, "get_embed_model",
                        lambda *a, **k: types.SimpleNamespace(
                            get_sentence_embedding_dimension=lambda: dims))
    assert m.vector_field_for() == expected, \
        f"vector_field_for() advertises {m.vector_field_for()!r}, expected {expected!r}"
    idx = m._get_rag_index("t29", None)
    vec = [f for f in idx.schema.fields.values() if str(f.type).endswith("vector")]
    assert len(vec) == 1, f"expected exactly one vector field, got {[f.name for f in vec]}"
    assert vec[0].name == expected, \
        f"schema indexes vector field {vec[0].name!r} while vector_field_for() " \
        f"advertises {expected!r} — a mixed-model index has nowhere to put this width"


def test_issue29_migration_moves_legacy_vectors(app_module, clean_redis, monkeypatch):
    """Renaming the field without moving existing vectors would leave the whole
    corpus indexed under a name nothing writes — searchable by nothing, silently."""
    m = app_module
    rc = clean_redis
    # Pin the model: other tests mutate _config, and the field name derives from it.
    monkeypatch.setitem(m._config, "embedding", {"model": "intfloat/multilingual-e5-small"})
    field = m.vector_field_for()
    assert field == "embedding_384", f"unexpected field {field}"
    keys = [rc.key(f"mig:chunk:{i}") for i in range(5)]
    for k in keys:
        rc.hset(k, mapping={"text": "t", "embedding": b"\x00" * 16})
    moved = m._migrate_vector_batch(rc, keys, field)
    assert moved == 5
    for k in keys:
        fields = {f.decode() for f in rc.hkeys(k)}
        assert field in fields and "embedding" not in fields
    # Idempotent: a second pass moves nothing.
    assert m._migrate_vector_batch(rc, keys, field) == 0


def test_index_ensure_is_serialised(app_module, monkeypatch):
    """Concurrent first-callers each ran the slow path. Harmless for FT.CREATE,
    not harmless once it moves 57k vectors — the migration ran twice for real.

    The old assertions were ``hasattr(m, "_index_ensure_locks")`` and
    ``"Lock()" in getsource(ensure_rag_index)``: both survive removing the lock
    from the call site. Eight threads now race the real entry point.
    """
    import threading
    m = app_module
    runs = []

    def slow_path(instance, rc, ver_key):
        runs.append(instance)
        time.sleep(0.2)          # long enough that unsynchronised callers overlap
        m._index_ensured.add(instance)   # what the real slow path does last

    monkeypatch.setattr(m, "_ensure_rag_index_locked", slow_path)
    monkeypatch.setattr(m, "_index_ensured", set())
    monkeypatch.setattr(m, "_index_ensure_locks", {})

    threads = [threading.Thread(target=m.ensure_rag_index, args=("race",), kwargs={"rc": object()})
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert runs == ["race"], \
        f"the migration ran {len(runs)} times for 8 concurrent first-callers, expected 1"
    assert "race" in m._index_ensured


# ── #24 / #25 resource bounds ────────────────────────────────────────────────

def test_issue24_upload_size_is_capped(app_module, data_dir, monkeypatch):
    """api_ingest_files reads the whole body into memory before touching disk.

    The old assertions only checked that _MAX_UPLOAD_BYTES exists and that its
    NAME appears in the handler — making the comparison unreachable (mutation
    M09: ``if False:  # _MAX_UPLOAD_BYTES``) left both true. This posts an
    oversized file through the real handler.
    """
    m = app_module
    monkeypatch.setattr(m, "_MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(m, "_rc_for", lambda *a, **k: object())
    dest = data_dir / "big.txt"
    monkeypatch.setattr(m, "safe_upload_dest", lambda name: (dest, "big.txt"))
    ingested = []

    async def fake_ingest(instance, path, name, rc):
        ingested.append(name)
        return {"ok": True}

    monkeypatch.setattr(m, "ingest_file", fake_ingest)

    class _Upload:
        filename = "big.txt"

        async def read(self):
            return b"x" * (m._MAX_UPLOAD_BYTES + 1)

    with pytest.raises(m.HTTPException) as ei:
        asyncio.run(m.api_ingest_files(instance="t24", files=[_Upload()]))
    assert ei.value.status_code == 413, f"oversized upload returned {ei.value.status_code}"
    assert not ingested, "the oversized file was ingested anyway"
    assert not dest.exists(), "the oversized body was written to disk before the check"

    # …and a file under the cap still goes through.
    class _Small(_Upload):
        async def read(self):
            return b"x" * 16

    assert asyncio.run(m.api_ingest_files(instance="t24", files=[_Small()])) == [{"ok": True}]


def test_issue25_sessions_are_bounded(app_module, monkeypatch):
    """Every conversation ever opened stayed resident, so RSS grew on uptime."""
    m = app_module
    monkeypatch.setattr(m, "_MAX_LIVE_SESSIONS", 5)
    saved = m._sessions
    try:
        from collections import OrderedDict
        m._sessions = OrderedDict()
        for i in range(20):
            m._sessions[f"s{i}"] = []
            m.touch_session(f"s{i}")
        assert len(m._sessions) == 5, f"{len(m._sessions)} sessions retained, cap is 5"
        assert "s19" in m._sessions and "s0" not in m._sessions, "wrong end evicted"
    finally:
        m._sessions = saved


def test_issue25_finished_crawls_are_reaped(app_module):
    """Completed crawl tasks were never removed, so each crawl leaked one — but a
    RUNNING crawl must be KEPT. Probe P17 (`done = list(_crawl_tasks)`) reaped
    everything; the old test registered only a finished task, so it never noticed a
    running one being dropped, and `>= 1` could not tell 'reaped the one done task'
    from 'reaped both'."""
    m = app_module

    class Done:
        def done(self): return True

    class Running:
        def done(self): return False

    m._crawl_tasks["http://run"] = Running()
    m._crawl_gates["http://run"] = object()
    m._crawl_tasks["http://x"] = Done()
    m._crawl_gates["http://x"] = object()
    try:
        assert m.reap_finished_crawls() == 1, "expected EXACTLY the finished crawl to be reaped"
        assert "http://x" not in m._crawl_tasks and "http://x" not in m._crawl_gates, \
            "the finished crawl was not reaped"
        assert "http://run" in m._crawl_tasks and "http://run" in m._crawl_gates, \
            "the reaper dropped a RUNNING crawl"
    finally:
        for u in ("http://run", "http://x"):
            m._crawl_tasks.pop(u, None)
            m._crawl_gates.pop(u, None)


def test_hnsw_ef_runtime_is_raised_above_the_default(app_module, monkeypatch):
    """RediSearch defaults EF_RUNTIME to 10. Measured against exact brute force on
    57,805 vectors that gave recall@10 of 0.839 — one query in six missed the true
    top-10 entirely — while EF=128 gave 1.000 and was FASTER (0.81 ms vs 1.19 ms
    median). A silent recall loss with no speed benefit.

    Drives search_rag with a recording VectorQuery instead of grepping its source.
    A same-line comment ``ef_runtime=10,  # ef_runtime=_HNSW_EF_RUNTIME`` (probe P16)
    keeps the substring alive for a comment-stripped source read (``_code`` drops
    only whole-line comments) yet passes 10 to the KNN leg; the value actually
    handed to VectorQuery cannot be faked that way.
    """
    import numpy as np
    m = app_module
    assert m._HNSW_EF_RUNTIME >= 64, "below 64 recall drops off"
    seen = {}

    class _RecordingVQ:
        def __init__(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop after construction")

    monkeypatch.setattr(m, "VectorQuery", _RecordingVQ)
    monkeypatch.setattr(m, "embed", lambda t, is_query=False: np.zeros(384, dtype=np.float32))
    try:
        m.search_rag("tef", "q", rc=object())
    except Exception:
        pass
    assert seen, "search_rag never constructed a VectorQuery — the KNN leg moved"
    assert seen.get("ef_runtime") == m._HNSW_EF_RUNTIME, \
        f"the KNN leg passed ef_runtime={seen.get('ef_runtime')}, not " \
        f"_HNSW_EF_RUNTIME ({m._HNSW_EF_RUNTIME})"
    # The KNN leg must query the field the helper advertises, not a hardcoded name.
    assert seen.get("vector_field_name") == m.vector_field_for(), \
        f"the KNN leg queries {seen.get('vector_field_name')!r} but vector_field_for() " \
        f"advertises {m.vector_field_for()!r}"


# ── render lanes added in 1.5.0 ──────────────────────────────────────────────

def _index_html(app_module):
    return open(app_module.__file__.replace("main.py", "index.html"), encoding="utf-8").read()


def _rich_lanes_src(html: str) -> str:
    """The RICH_LANES object literal, bounded by its OWN closing brace.

    ``html[html.index("const RICH_LANES={"):]`` ran to end of file — 92,322 chars.
    Renaming the real lane and pasting a matching key (in syntactically broken JS)
    anywhere later satisfied it (mutation M60).
    """
    start = html.index("const RICH_LANES={")
    return html[start:html.index("\n};\n", start) + 3]


# Every registered lane (the full RICH_LANES key list, pinned in test_lanes_js.py
# test_rich_lanes_parses_and_holds_every_lane). The old parametrize guarded only
# four of these, so renaming e.g. the ```molecule fence (probe P14) survived — that
# lane simply had no test. Guard all nineteen.
_ALL_LANES = ["mermaid", "chart", "gantt", "timeline", "network", "geojson", "dot",
              "geometry", "map", "plot3d", "calc", "solve", "stats", "truth",
              "table", "diff", "regex", "molecule", "molecule3d"]


@pytest.mark.parametrize("lane", _ALL_LANES)
def test_new_lane_is_registered(app_module, lane):
    """A lane the base instruction advertises but RICH_LANES lacks renders as a
    plain code block — the model emits it and the user sees raw JSON."""
    reg = _rich_lanes_src(_index_html(app_module))
    assert f"\n  {lane}:{{" in reg, f"{lane} is not in RICH_LANES"


@pytest.mark.parametrize("lane", _ALL_LANES)
def test_new_lane_is_documented_for_the_model(app_module, lane):
    """RICH_LANES and the base instruction have to agree: a lane the model is
    never told about is dead code.

    ``f"```{lane}" in src`` was satisfied by an incidental CROSS-REFERENCE: the
    mermaid bullet reads "for Gantt prefer the dedicated ```gantt fence", so
    deleting the gantt bullet outright kept it green (mutations M81/M82). Require
    the lane's own entry in the fence catalogue.
    """
    src = open(app_module.__file__, encoding="utf-8").read()
    assert f"- ```{lane} —" in src, \
        f"the fence catalogue has no ```{lane} entry — the model will never emit it"


def test_gantt_sets_a_tick_interval(app_module):
    """Twice regressed. mermaid's default gantt ticks land every few days, which
    at card width collide into an unreadable band; and without an explicit width
    it measures its detached container as 0 and emits viewBox='0 0 0 h'."""
    html = _index_html(app_module)
    blk = html[html.index("  gantt:{"):]
    # Comments stripped: the block's own prose explains why useWidth is needed, so
    # `"useWidth" in blk` stayed true after the real key was deleted (mutation M46).
    blk = _code(blk[:blk.index("  timeline:{")])
    assert "tickInterval:'" in blk, "no tickInterval — the date axis will smear"
    assert "useWidth:" in blk, "no useWidth — mermaid will render a zero-width chart"
    assert "useMaxWidth:false" in blk, "useMaxWidth:true re-fits the chart to a 0-wide container"


def test_viewbox_sizing_helper_is_wired(app_module):
    """mermaid's timeline emits a viewBox with no width/height. As a flex item it
    lays out 0x0, so the card looks empty although the content is correct."""
    html = _index_html(app_module)
    assert "function _sizeFromViewBox" in html
    # Slice to the NEXT lane rather than a fixed window: the gantt block grew past
    # a 900-char cut-off once and the assertion started passing on the wrong text.
    for lane, nxt in (("gantt", "  timeline:{"), ("timeline", "  network:{")):
        start = html.index(f"  {lane}:{{")
        blk = html[start:html.index(nxt, start)]
        assert "_sizeFromViewBox(out)" in blk, f"{lane} never calls the sizing helper"


# Every external asset index.html loads, HEAD-checked for HTTP 200 on 2026-08-07
# (24/24). This is the CONTRACT: adding, removing or repointing an asset must
# update this list in the same commit, and test_manifest_urls_are_live (network,
# opt-in) is what proves an entry is real.
#
# The previous test named nine of these paths as literal `in html` comparisons and
# its docstring claimed HTTP verification it never performed. Three real 404s went
# straight through it: KaTeX/0.17.0/dist/katex.min.js, KaTeX/0.17.0/auto-render.min.js
# and mermaid/11.12.0/dist/mermaid.min.js (mutations M18, M51, M19 — all measured
# 404 against the live CDN, all SURVIVED). Comparing the whole extracted SET closes
# that: any path change at all is a set difference.
_CDNJS = "https://cdnjs.cloudflare.com/ajax/libs/"
CDN_MANIFEST = frozenset([
    "https://cdn.jsdelivr.net/npm/@viz-js/viz@3.29.0/dist/viz-global.js",
    "https://cdn.jsdelivr.net/npm/smiles-drawer@2.4.1/dist/smiles-drawer.min.js",
    _CDNJS + "3Dmol/2.5.5/3Dmol-min.js",
    _CDNJS + "Chart.js/4.5.0/chart.umd.min.js",
    _CDNJS + "KaTeX/0.17.0/contrib/auto-render.min.js",
    _CDNJS + "KaTeX/0.17.0/katex.min.css",
    _CDNJS + "KaTeX/0.17.0/katex.min.js",
    _CDNJS + "abcjs/6.6.4/abcjs-basic-min.min.js",
    _CDNJS + "chartjs-plugin-zoom/2.2.0/chartjs-plugin-zoom.min.js",
    _CDNJS + "dompurify/3.4.11/purify.min.js",
    _CDNJS + "hammer.js/2.0.8/hammer.min.js",
    _CDNJS + "highlight.js/11.11.1/highlight.min.js",
    _CDNJS + "highlight.js/11.11.1/styles/github-dark.min.css",
    _CDNJS + "highlight.js/11.11.1/styles/github.min.css",
    _CDNJS + "jsxgraph/1.12.2/jsxgraph.css",
    _CDNJS + "jsxgraph/1.12.2/jsxgraphcore.js",
    _CDNJS + "leaflet/1.9.4/leaflet.css",
    _CDNJS + "leaflet/1.9.4/leaflet.js",
    _CDNJS + "marked/16.3.0/lib/marked.umd.js",
    _CDNJS + "mathjs/15.1.0/math.min.js",
    _CDNJS + "mermaid/11.12.0/mermaid.min.js",
    _CDNJS + "plotly.js/3.1.1/plotly.min.js",
    # vis-network really does publish its CSS under dist/dist/ — the single-dist
    # variant 404s. Verified both ways.
    _CDNJS + "vis-network/10.1.0/dist/dist/vis-network.min.css",
    _CDNJS + "vis-network/10.1.0/dist/vis-network.min.js",
])


def _cdn_asset_urls(html: str) -> set[str]:
    """Every external .js/.css index.html pulls, however the URL is spelled."""
    cdn = re.search(r"const CDN\s*=\s*'([^']+)'", html).group(1)
    urls = set()
    # 1. literal src=/href= attributes
    for m in re.finditer(r'''(?:src|href)=["'](https?://[^"']+\.(?:js|css))["']''', html):
        urls.add(m.group(1))
    # 2. _loadScript(...) / _loadCss(...), with or without the CDN prefix
    for m in re.finditer(r'''_load(?:Script|Css)\(\s*(CDN\s*\+\s*)?['"]([^'"]+\.(?:js|css))['"]''', html):
        urls.add((cdn if m.group(1) else "") + m.group(2))
    # 3. stylesheets assembled in a loop: ['a','b'].forEach(n=>{ … href=CDN+'p/'+n+'.css' })
    for m in re.finditer(r"\[((?:'[^']+',?)+)\]\.forEach\(\s*(\w+)\s*=>", html):
        names, var = re.findall(r"'([^']+)'", m.group(1)), m.group(2)
        for c in re.finditer(r"href\s*=\s*CDN\s*\+\s*'([^']*)'\s*\+\s*" + var + r"\s*\+\s*'([^']*)'",
                             html[m.end():m.end() + 2000]):
            urls.update(cdn + c.group(1) + n + c.group(2) for n in names)
    return urls


def test_lane_cdn_paths_are_the_verified_ones(app_module):
    """A CDN path that 404s kills its whole lane silently — the script tag simply
    never resolves and the renderer is never defined."""
    html = _index_html(app_module)
    found = _cdn_asset_urls(html)
    # Guard the extractor itself: a regex that stops matching would otherwise turn
    # this into a vacuous comparison of two empty sets.
    assert len(found) == 24, f"extractor found {len(found)} asset URLs, expected 24"
    assert found == set(CDN_MANIFEST), (
        "index.html's asset URLs no longer match the HEAD-verified manifest.\n"
        f"  only in index.html: {sorted(found - CDN_MANIFEST)}\n"
        f"  only in manifest  : {sorted(CDN_MANIFEST - found)}\n"
        "If the change is intentional, update CDN_MANIFEST and re-run "
        "REDIRECALL_TEST_NETWORK=1 pytest -k manifest_urls_are_live."
    )
    # len==24 only proves the extractor still finds the KNOWN assets; it is blind to
    # one loaded through an EXPRESSION the extractor cannot resolve. A `${CDN}…` template
    # literal (probe P07) or `_loadScript(CDN+VAR)` (probe P11) would never be
    # HEAD-verified yet ship. Every _loadScript/_loadCss call must take a quoted literal.
    unresolvable = []
    for mo in re.finditer(r"_load(?:Script|Css)\(", html):
        arg = html[mo.end():mo.end() + 120]
        if arg.startswith("url)"):          # the function definition, not a call
            continue
        if not re.match(r"""\s*(?:CDN\s*\+\s*)?['"][^'"]+\.(?:js|css)['"]""", arg):
            unresolvable.append(arg.split("\n")[0][:70])
    assert not unresolvable, (
        "a CDN asset is loaded through an expression _cdn_asset_urls() cannot resolve "
        f"(so it is never HEAD-verified): {unresolvable}"
    )


def test_retired_cdn_paths_stay_retired(app_module):
    """Near-misses that are easy to reintroduce and that all 404."""
    html = _index_html(app_module)
    assert "hammerjs/" not in html, "hammerjs/ 404s — the package is hammer.js/"
    assert re.search(r'src="[^"]*marked\.min\.js"', html) is None, \
        "marked/<ver>/marked.min.js 404s since v16"
    assert "KaTeX/0.16" not in html, "a stale KaTeX path would mismatch the metrics"
    assert "viz.js/2.1.2" not in html, "the abandoned asm.js build is retired"


@pytest.mark.network
def test_manifest_urls_are_live(app_module):
    """HEAD every manifest URL. This is the check the old docstring claimed.

    Opt-in (REDIRECALL_TEST_NETWORK=1) rather than default-on: the mutation sweep
    runs the suite ~70 times and would issue ~1,700 requests to cdnjs. Run it
    whenever CDN_MANIFEST changes; measured 24/24 → 200 on 2026-08-07.
    """
    import urllib.error
    import urllib.request
    if not os.environ.get("REDIRECALL_TEST_NETWORK"):
        pytest.skip("set REDIRECALL_TEST_NETWORK=1 to HEAD the CDN manifest")
    bad = []
    unreachable = []
    for url in sorted(CDN_MANIFEST):
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "redirecall-tests/1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    bad.append(f"{resp.status} {url}")
        except urllib.error.HTTPError as e:
            bad.append(f"{e.code} {url}")
        except Exception as e:                       # pragma: no cover
            unreachable.append(f"{e} {url}")
    # Decide AFTER the loop. The old in-loop `pytest.skip` on the first unreachable
    # host discarded every 404 already collected — a genuine 404 alongside one
    # timeout reported SKIPPED, hiding a dead asset. A real 404 must FAIL.
    if bad:
        pytest.fail("CDN assets that do not return 200:\n  " + "\n  ".join(bad))
    if unreachable:
        pytest.skip(f"network unreachable for {len(unreachable)} url(s)")


def test_abc_playback_host_is_allowed_by_csp(app_module):
    """abcjs fetches soundfont samples at play time. With connect-src 'self' the
    fetch is blocked and playback fails silently.

    Parses the SERVED policy (``app_module._CSP``), not a fixed-width window of the
    source file. The old ``src[i:i+1400]`` slice also spanned the prose comment two
    lines above the directive that names the host, so dropping the host from the
    directive while keeping the comment (probe P01) left ``paulrosen.github.io`` in
    the window and the test green. A directive value carries no comment.
    """
    directives = _csp_directives(app_module)
    assert _csp_permits(directives, "connect-src", "https://paulrosen.github.io"), \
        f"connect-src does not permit the abc soundfont host; " \
        f"connect-src = {directives.get('connect-src')!r}"
    html = _index_html(app_module)
    assert "data-act=\"abc-play\"" in html
    assert "out._abcTune=" in html, "the parsed tune is never stored, so nothing can play"


# ── table enhancements ───────────────────────────────────────────────────────

def test_enhance_tables_runs_on_every_render_path(app_module):
    """Four separate chains put a table on screen (stream finalize, restore,
    version switch, chart Data view). A chain that skips it loses sorting.

    ``html.count("enhanceTables(") >= 4`` was off by one: the true count is 5
    because the DEFINITION counts too, so losing exactly one call site (mutation
    M20 — restored conversations) passed. Anchor on the four owners instead, which
    also survives a legitimate fifth call site being added.
    """
    html = _index_html(app_module)
    for owner in ("function finalizeStreamingMsg(", "function appendMessage(",
                  "function showVersion("):
        assert "enhanceTables(" in _js_fn(html, owner), f"{owner} does not call enhanceTables"
    # The chart Data view is a branch of the delegated click handler, not a function.
    branch = html[html.index("}else if(act==='chart-data'){"):]
    branch = _code(branch[:branch.index("}else if(act===", 10)])
    assert "enhanceTables(" in branch, "the chart Data table renders unsortable"


def test_enhance_tables_does_not_skip_the_chart_data_view(app_module):
    """The guard excluded anything inside .rich-wrap, which is exactly where the
    chart Data table lives — it rendered unsortable."""
    html = _index_html(app_module)
    # Slice the WHOLE function (3,283 chars): the old [:900] window covered only 27%,
    # so re-adding `if(bar.closest('.rich-wrap')) return;` at offset 1,514 (probe
    # P09b) sat past the window and the test stayed green.
    fn = _js_fn(html, "function enhanceTables(")
    assert "closest('.rich-output')" in fn, "guard must scope to .rich-output, not .rich-wrap"
    assert "closest('.rich-wrap')" not in fn, \
        "the .rich-wrap guard is back — the chart Data table renders unsortable"


def test_table_sort_coerces_values_by_type():
    """Currency and integers must sort by value. Lexically '120' < '43' and
    '$1,200.50' < '$980.00', which is the bug this guards."""
    import json
    import shutil
    import subprocess
    import pathlib
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"
    t = html.read_text(encoding="utf-8")
    start = t.index("const _RR_NUM=")
    end = t.index("function _rrTableCsv(")
    js = t[start:end] + """
const cases=["$1,200.50","$980.00","$210.10","$3,400.75","120","43","7","88","2026-03-01","2026-01-15","apple","Banana"];
const out=cases.map(s=>_rrCellVal({textContent:s}));
console.log(JSON.stringify(out));
"""
    r = run_node(js, timeout=60)
    assert r.returncode == 0, r.stderr[:300]
    v = json.loads(r.stdout)
    money = v[0:4]
    assert all(isinstance(x, (int, float)) for x in money), f"currency not numeric: {money}"
    assert sorted(money) == [210.10, 980.00, 1200.50, 3400.75]
    ints = v[4:8]
    assert sorted(ints) == [7, 43, 88, 120], f"integers sorted lexically: {sorted(ints)}"
    assert isinstance(v[8], (int, float)) and v[8] > v[9], "dates not compared chronologically"
    assert v[10] == "apple" and v[11] == "banana", "strings must fold case for sorting"


# ── C1/C2: CSP host coverage and third-party-licence completeness ─────────────
# The 1.5.0 upgrade opened connect-src for the abc soundfont and added two chart
# libraries (hammer.js, chartjs-plugin-zoom), but the docs kept promising the old
# guarantee and never listed the new libraries — and nothing guarded any of it.
# These tests tie the SERVED CSP and THIRD-PARTY-LICENSES.md back to what
# index.html actually loads, so a host the CSP forgets, or a library the licence
# file omits, fails here rather than in a user's browser or a licence audit.
import pathlib
from urllib.parse import urlparse

_DOCS_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _norm(s: str) -> str:
    """Fold to a comparison key: lowercase, keep only [a-z0-9].

    So the cdnjs slug ``mathjs`` matches the doc's ``math.js``, ``plotly.js``
    matches ``Plotly.js`` and ``@viz-js/viz`` matches ``@viz-js/viz`` — a
    spelling/punctuation gap between the URL and the prose must not hide a
    missing entry.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _csp_directives(app_module) -> dict:
    """The SERVED policy (``app_module._CSP`` — the real header value, not the
    source text) parsed into ``directive -> [source tokens]``."""
    out = {}
    for part in app_module._CSP.split(";"):
        part = part.strip()
        if not part:
            continue
        name, *tokens = part.split()
        out[name] = tokens
    return out


def _csp_permits(directives: dict, directive: str, host: str) -> bool:
    """Does ``directive`` (falling back to default-src) allow loads from ``host``?

    A host-source token carries the host verbatim (``https://cdnjs.cloudflare.com``);
    ``'self'``/``'unsafe-inline'``/bare-scheme sources name no host, so testing the
    specific host as a substring is exact enough and never matches those by accident.
    """
    tokens = directives.get(directive) or directives.get("default-src", [])
    return any(host in tok for tok in tokens)


def _index_resource_requirements(html: str) -> set:
    """``(host, csp_directive)`` pairs index.html actually needs — derived from the
    page, never hand-listed: every ``.js`` is a script-src load, every ``.css`` a
    style-src load, and the abcjs soundfont a connect-src fetch."""
    reqs = set()
    for url in _cdn_asset_urls(html):
        host = urlparse(url).netloc
        reqs.add((host, "style-src" if url.endswith(".css") else "script-src"))
    m = re.search(r"_ABC_SOUNDFONT\s*=\s*'(https?://[^']+)'", html)
    if m:
        reqs.add((urlparse(m.group(1)).netloc, "connect-src"))
    return reqs


def test_every_cdn_host_in_index_is_allowed_by_csp(app_module):
    """A renderer host the CSP omits is silently fatal: the browser blocks the
    load and the lane never defines itself.

    Session-introduced shape: connect-src was ``'self'``-only until the abc
    soundfont host was added; a host added to index.html but not to ``_CSP`` would
    repeat that. Checks the served ``_CSP`` value against every host the page
    loads from, so removing e.g. cdn.jsdelivr.net from script-src (viz/smiles) or
    paulrosen.github.io from connect-src is caught here."""
    reqs = _index_resource_requirements(_index_html(app_module))
    # Guard the extractor: an empty derivation would make the check vacuous.
    assert ("cdn.jsdelivr.net", "script-src") in reqs, "asset extractor missed jsDelivr"
    assert ("paulrosen.github.io", "connect-src") in reqs, "soundfont host not derived"
    directives = _csp_directives(app_module)
    blocked = sorted(f"{host} (needs {d})" for host, d in reqs
                     if not _csp_permits(directives, d, host))
    assert not blocked, (
        "index.html loads from hosts the CSP does not permit — the browser blocks "
        "the load and the lane dies:\n  " + "\n  ".join(blocked))


def _loaded_library_slugs(html: str) -> dict:
    """``slug -> a representative URL`` for every distinct library index.html loads.

    cdnjs paths are ``…/ajax/libs/<slug>/<ver>/…``; jsDelivr are
    ``…/npm/<pkg>@<ver>/…`` (pkg may be scoped, ``@viz-js/viz``). The slug is the
    library's package identity, which is what has to be attributed."""
    out = {}
    for url in _cdn_asset_urls(html):
        if "/ajax/libs/" in url:
            slug = url.split("/ajax/libs/", 1)[1].split("/", 1)[0]
        elif "/npm/" in url:
            rest = url.split("/npm/", 1)[1]
            slug = ("@" + rest[1:].split("@", 1)[0]) if rest.startswith("@") \
                else rest.split("@", 1)[0]
        else:
            continue
        out.setdefault(slug, url)
    return out


def test_every_loaded_library_is_in_third_party_licenses(app_module):
    """C2: hammer.js and chartjs-plugin-zoom were loaded but unlisted, so the
    licence inventory was simply wrong. Every package index.html pulls must appear
    in THIRD-PARTY-LICENSES.md (matched on a punctuation-folded key so ``mathjs``
    finds ``math.js``)."""
    slugs = _loaded_library_slugs(_index_html(app_module))
    # Guard the extractor against a regex regression turning this vacuous.
    assert {"@viz-js/viz", "smiles-drawer", "hammer.js"} <= set(slugs), \
        f"library extractor is broken; got {sorted(slugs)}"
    ndoc = _norm((_DOCS_ROOT / "THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8"))
    missing = sorted(s for s in slugs if _norm(s) not in ndoc)
    assert not missing, (
        "libraries index.html loads but THIRD-PARTY-LICENSES.md omits: "
        f"{missing}  (e.g. {[slugs[m] for m in missing]})")


def test_fluidr3_soundfont_is_attributed(app_module):
    """CC-BY-3.0 requires attribution and 1.5.0 shipped none. abcjs fetches the
    FluidR3_GM soundfont at play time, so the licence file must credit the author
    (Frank Wen) under CC-BY-3.0 and name the host. The old file also called OSM
    tiles 'the only render type that contacts a third party', which this very
    fetch (paulrosen.github.io) falsified."""
    html = _index_html(app_module)
    m = re.search(r"_ABC_SOUNDFONT\s*=\s*'(https?://[^']+)'", html)
    assert m, "the abc soundfont constant is gone — re-target this test"
    host = urlparse(m.group(1)).netloc
    doc = (_DOCS_ROOT / "THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8")
    ndoc = _norm(doc)
    assert "fluidr3" in ndoc, "FluidR3_GM soundfont is not attributed at all"
    assert "frankwen" in ndoc, "CC-BY-3.0 attribution must credit the author, Frank Wen"
    assert "ccby30" in ndoc or "creativecommonsattribution30" in ndoc, \
        "the CC-BY-3.0 licence the soundfont is under is not named"
    assert _norm(host) in ndoc, f"the soundfont host ({host}) it is fetched from is undocumented"
    assert "only render type that contacts a third party" not in doc, \
        "the abc soundfont also contacts a third party, so the OSM 'only' claim is false"


def test_graphviz_documented_as_epl2_not_epl1(app_module):
    """C2: viz.js embeds Graphviz 15.1.1, relicensed to EPL-2.0 at 14.1.4 (early
    2026). readme.md and DOCS.md still labelled it EPL-1.0; THIRD-PARTY-LICENSES.md
    dated the switch to 'since 15.x'."""
    for name in ("readme.md", "DOCS.md"):
        text = (_DOCS_ROOT / name).read_text(encoding="utf-8")
        assert "EPL-2.0" in text or "Eclipse Public License 2.0" in text, \
            f"{name}: Graphviz is not documented as EPL-2.0"
        assert "EPL-1.0" not in text, f"{name}: stale 'EPL-1.0' label still present"
        assert "Eclipse Public License 1.0" not in text, \
            f"{name}: stale 'Eclipse Public License 1.0' still present"
    tpl = (_DOCS_ROOT / "THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8")
    assert "14.1.4" in tpl, "the corrected EPL-2.0 switch version (14.1.4) is missing"
    assert "since 15.x" not in tpl, "the false 'switch happened since 15.x' claim is back"


def test_readme_csp_claim_matches_connect_src(app_module):
    """C1: readme.md's Security section promised connect-src was ``'self'``-only and
    therefore 'the only' non-img exfiltration channel. The soundfont host opened
    connect-src, so the guarantee was false as written."""
    text = (_DOCS_ROOT / "readme.md").read_text(encoding="utf-8")
    sec = text[text.index("## Security"):text.index("## Documentation")]
    assert "restricted to `'self'`, so it is the only one" not in sec, \
        "readme still claims connect-src is restricted to 'self'"
    assert "paulrosen.github.io" in sec, \
        "readme's CSP note must name the host connect-src actually permits"
    # …and it must agree with the served policy: that host really is in connect-src.
    assert _csp_permits(_csp_directives(app_module), "connect-src", "paulrosen.github.io"), \
        "connect-src no longer permits the host the readme documents"
