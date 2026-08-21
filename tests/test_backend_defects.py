# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regressions for backend defects that were latent in shipped code.

Each test names the failure it guards; none of these paths had coverage, which is
why every one of them survived a release. All offline — no network, no live crawl.
"""
import ast
import asyncio
import pathlib
import types

import numpy as np

import pytest

from redirecall import crawler, embeddings, providers, rag, routes_ingestion, routes_media

_SRC = pathlib.Path(__file__).resolve().parents[1] / "redirecall"


# ── glob injection via an instance name ──────────────────────────────────────
@pytest.mark.parametrize("name,expect_widened", [
    ("normal", False), ("with-dash", False), ("with.dot", False), ("a_b", False),
    ("*", True), ("?", True), ("[a-z]", True), ("pre*fix", True),
])
def test_instance_name_never_widens_the_chunk_scan_pattern(name, expect_widened):
    """The chunk SCAN pattern is built by interpolating the instance name. An instance
    called "*" turned `rag:*:chunk:*` into every chunk of every instance, so resetting
    or deleting that one took all the others with it."""
    pat = rag.rag_chunk_glob(name)
    # Every glob metacharacter contributed by the NAME must arrive escaped. The only
    # unescaped '*' allowed is the trailing one this pattern owns.
    body = pat[len("rag:"):-len(":chunk:*")]
    for meta in "*?[]":
        for i, ch in enumerate(body):
            if ch == meta:
                assert i > 0 and body[i - 1] == "\\", \
                    f"unescaped {meta!r} from the instance name in {pat!r}"
    assert pat.endswith(":chunk:*")
    if not expect_widened:
        assert "\\" not in body, f"an ordinary name must not be mangled: {pat!r}"


@pytest.mark.parametrize("name", ["*", "?", "[a-z]", "pre*fix", "a]b"])
def test_the_prefix_glob_helper_escapes_too(name):
    """chunk_glob_for_prefix is the sibling used by the export reader and the source scan
    (they receive a built prefix, not the instance name). It was never covered, so its
    escaping could be dropped with the whole suite green."""
    pat = rag.chunk_glob_for_prefix(rag.rag_prefix(name))
    body = pat[len("rag:"):-len(":chunk:*")]
    for i, ch in enumerate(body):
        if ch in "*?[]":
            assert i > 0 and body[i - 1] == "\\", f"unescaped {ch!r} in {pat!r}"


def test_both_glob_helpers_agree_for_the_same_instance():
    for name in ("default", "a-b", "*", "[x]"):
        assert rag.rag_chunk_glob(name) == rag.chunk_glob_for_prefix(rag.rag_prefix(name))


def test_instance_name_validator_rejects_glob_and_separator_characters():
    """Only characters that MEAN something to the Redis pattern matcher are rejected.
    An earlier draft also barred spaces, slashes and non-ASCII letters, which carry no
    glob meaning — that turned a security check into an arbitrary naming policy and
    refused perfectly safe names like "My Docs"."""
    from fastapi import HTTPException
    from redirecall import routes_instances as ri
    for good in ("default", "product-docs", "kb_2", "a.b", "A1",
                 "My Docs", "ünïcode", "a/b", " lead"):
        assert ri._check_instance_name(good) == good
    for bad in ("*", "a*b", "a:b", "", "   ", "[x]", "a]b", "?", "a\\\\b",
                "x" * 65, None, 5):
        with pytest.raises(HTTPException):
            ri._check_instance_name(bad)


def test_every_route_that_can_create_an_instance_validates_its_name():
    """Gating only POST /api/rag/instances left POST /api/rag/*/import able to create one
    called "*". Every route that can bring an instance into existence must check."""
    for mod, names in (("routes_ingestion.py", ("api_import_rag", "api_ingest_files",
                                                "api_ingest_files_stream")),
                       # the original call site — omitting it meant the gate could be
                       # removed from instance creation itself with the suite still green
                       ("routes_instances.py", ("api_create_instance",))):
        tree = ast.parse((_SRC / mod).read_text())
        for name in names:
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
            assert "_check_instance_name" in ast.unparse(fn), \
                f"{mod}::{name} does not validate the instance name"


# ── UnboundLocalError in search_rag ──────────────────────────────────────────
def test_search_rag_does_not_call_a_bare_r():
    """`r` is a loop variable inside search_rag, which makes Python treat it as local
    for the whole body — so a bare `r()` there raised UnboundLocalError on every caller
    that left `rc` unset. Latent only because all in-repo callers pass one."""
    tree = ast.parse((_SRC / "rag.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "search_rag")
    bare = [c.lineno for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "r"]
    assert not bare, f"bare r() call(s) at line(s) {bare} — use redis_store.r()"


# ── regenerate truncation ────────────────────────────────────────────────────
def test_replace_from_rejects_a_boolean():
    """bool subclasses int, so a client-supplied JSON `true` passed the isinstance check
    as 1, walked back to index 0, and deleted the whole conversation.

    This calls the REAL predicate. The previous version asserted on the AST (which cannot
    see polarity — a guard inverted to `or isinstance(v, bool)` still matched) and then
    re-implemented the rule inside the test, which passed with routes_chat.py deleted.
    """
    from redirecall.routes_chat import valid_replace_index as ok
    assert ok(0, 4) and ok(2, 4) and ok(4, 4)
    assert not ok(True, 4) and not ok(False, 4), "a JSON true is accepted as index 1"
    assert not ok("2", 4) and not ok(None, 4) and not ok(2.0, 4)
    assert not ok(-1, 4) and not ok(5, 4)


def test_the_regenerate_path_uses_that_predicate():
    """Guards the wiring, not just the helper: an unused predicate protects nothing."""
    src = (_SRC / "routes_chat.py").read_text()
    assert "valid_replace_index(replace_from" in src, \
        "the regenerate truncation no longer goes through the validated predicate"


# ── image serving: prefix vs containment ─────────────────────────────────────
def test_image_route_rejects_a_sibling_directory_with_an_allowed_prefix(tmp_path, monkeypatch):
    """`str(p).startswith(str(allowed))` is not containment: with /tmp allowed,
    /tmpevil/x.png passes. Driven through the real route — the previous version asserted
    properties of pathlib itself (which pass with routes_media.py deleted) plus a negative
    substring that a differently-spelled revert evades."""
    from fastapi import HTTPException
    allowed = tmp_path / "ok"; allowed.mkdir()
    sibling = tmp_path / "okevil"; sibling.mkdir()      # defeats a prefix test
    (allowed / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sibling / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert str(sibling).startswith(str(allowed)), "the fixture does not defeat a prefix test"

    monkeypatch.setattr(routes_media, "_ALLOWED_IMAGE_DIRS", [allowed])

    def call(p):
        # the route is async — an un-awaited coroutine raises nothing and asserts nothing
        return _run_async(lambda: routes_media.api_serve_image(path=str(p)))

    served = call(allowed / "a.png")            # inside the allowed directory
    assert served is not None and getattr(served, "path", None) == str(allowed / "a.png")

    with pytest.raises(HTTPException) as ei:    # sibling with an allowed-looking prefix
        call(sibling / "a.png")
    assert ei.value.status_code == 403, f"expected 403, got {ei.value.status_code}"


# ── tool-result markdown: for/else always fired ──────────────────────────────
def test_tool_result_with_an_image_does_not_also_dump_the_raw_json():
    """The image branches `continue`, which does not skip a for/else — so the raw JSON
    (including the entire base64 data URI) was appended after every image."""
    out = providers._tool_result_to_markdown(
        [{"function": {"name": "draw", "arguments": {"img": "data:image/png;base64,AAAA"}}}])
    assert "![draw result](data:image/png;base64,AAAA)" in out
    assert "```json" not in out, f"raw tool JSON emitted alongside the image: {out!r}"


def test_tool_result_without_an_image_still_emits_the_raw_json():
    out = providers._tool_result_to_markdown(
        [{"function": {"name": "lookup", "arguments": {"q": "weather"}}}])
    assert "```json" in out and "weather" in out


# ── usage reporting must survive an unrelated 400 ────────────────────────────
class _Boom(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status_code = status


def _client(fail_first_only, msg, status=400):
    """chat.completions.create that raises once (or always) with a given error."""
    calls = []

    class _C:
        async def create(self, **kw):
            calls.append(kw)
            if "stream_options" in kw or not fail_first_only:
                raise _Boom(msg, status)

            class _S:
                def __aiter__(self):
                    async def g():
                        return
                        yield
                    return g()
            return _S()

    c = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_C()))
    c.calls = calls
    return c


def _run_async(coro_fn):
    """Run a coroutine on a fresh loop and CLOSE it.

    new_event_loop() without close() leaks a kqueue descriptor per call; these helpers are
    invoked dozens of times per suite run, and the project has already had one
    FD-exhaustion incident.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def _drain(client, key):
    async def go():
        async for _ in providers._openai_compat_stream(key, client, "m", [], {},
                                                       want_stream_options=True):
            pass
    return _run_async(go)


def test_a_400_about_something_else_does_not_disable_usage_reporting():
    """A mistyped model name, an over-long context and a malformed image are all 400s.
    Treating any 400 as 'this endpoint rejects stream_options' permanently disabled
    token counting for the whole provider — the feature the user asked to track."""
    providers._NO_STREAM_OPTIONS.discard("t_ctx")
    client = _client(False, "context_length_exceeded: too many tokens")
    with pytest.raises(Exception):
        _drain(client, "t_ctx")
    assert "t_ctx" not in providers._NO_STREAM_OPTIONS, \
        "an unrelated 400 permanently switched off usage reporting"


def test_a_400_naming_the_parameter_still_demotes():
    providers._NO_STREAM_OPTIONS.discard("t_par")
    client = _client(True, "unknown parameter: stream_options")
    _drain(client, "t_par")
    assert "t_par" in providers._NO_STREAM_OPTIONS
    providers._NO_STREAM_OPTIONS.discard("t_par")


def test_a_bare_400_that_succeeds_on_retry_is_not_remembered():
    """No evidence the endpoint dislikes the parameter — so retry next turn rather
    than poisoning the process for its lifetime."""
    providers._NO_STREAM_OPTIONS.discard("t_bare")
    client = _client(True, "Bad Request")
    _drain(client, "t_bare")
    assert "t_bare" not in providers._NO_STREAM_OPTIONS
    assert "stream_options" in client.calls[0] and "stream_options" not in client.calls[1]


# ── API keys must never travel in a URL ──────────────────────────────────────
@pytest.mark.parametrize("provider", ["claude", "openai", "qwen", "mistral", "groq", "gemini"])
def test_provider_status_never_takes_the_api_key_from_the_query_string(provider):
    """A query string is written to the server access log, to every proxy log in front
    of it, and to the browser's own history — so the six calls whose entire purpose is
    handling a credential were the ones broadcasting it. The key moves to the body."""
    from redirecall import routes_settings as rs
    fn = getattr(rs, f"api_{provider}_status")
    import inspect
    params = inspect.signature(fn).parameters
    assert "key" not in params, f"{provider} still accepts ?key= as a query parameter"
    assert "request" in params, f"{provider} does not read the key from the request body"
    src = inspect.getsource(fn)
    assert "_probe_key(request)" in src
    # ...and the route must actually accept both methods. Narrowing it to GET left every
    # Settings "Test" button returning 405 with the whole suite green.
    from redirecall import appcore
    methods = {m for r in appcore.app.routes
               if getattr(r, "path", None) == f"/api/status/{provider}"
               for m in getattr(r, "methods", set())}
    assert {"GET", "POST"} <= methods, \
        f"/api/status/{provider} registers {methods or 'nothing'}, needs GET and POST"


def test_probe_key_ignores_the_redacted_sentinel_and_non_post():
    """The Settings form pre-fills a saved key's field with the redaction sentinel and
    the Test button sends whatever is in the field, so the sentinel must fall through to
    the stored key rather than be tried as one."""
    from redirecall import config
    from redirecall import routes_settings as rs

    class _Req:
        def __init__(self, method, payload): self.method, self._p = method, payload
        async def json(self): return self._p

    run = lambda r: _run_async(lambda: rs._probe_key(r))
    assert run(_Req("POST", {"key": "sk-real"})) == "sk-real"
    assert run(_Req("POST", {"key": config._SECRET_SENTINEL})) is None
    assert run(_Req("POST", {"key": ""})) is None
    assert run(_Req("POST", {})) is None
    assert run(_Req("GET", {"key": "sk-real"})) is None, "GET must never carry a key"


def test_frontend_test_buttons_do_not_put_the_key_in_a_url():
    """The key must not reach the URL by ANY spelling. Matching one literal
    (`key=${encodeURIComponent`) only detected the exact revert: rebuilding the same query
    with string concatenation, or with URLSearchParams, put the secret straight back in
    the URL and kept the test green.

    So: the six handlers must each delegate to _statusProbe and never name `formKey` in a
    URL of their own, and the helper must put it in the BODY.
    """
    html = (_SRC / "index.html").read_text(encoding="utf-8")

    # 1. every Test handler delegates, and none builds its own request
    for provider in ("claude", "openai", "qwen", "mistral", "groq", "gemini"):
        call = f"_statusProbe('{provider}'"
        assert call in html, f"{provider}: the Test button no longer uses _statusProbe"
        i = html.index(call)
        body = html[max(0, i - 900):i]          # the handler above the call
        body = body[body.rindex("async function"):] if "async function" in body else body
        assert "/api/status/" not in body, \
            f"{provider}: the handler builds its own /api/status request instead of delegating"

    # 2. the helper is the ONLY place that builds the request, and the key goes in the body
    h = html[html.index("async function _statusProbe("):]
    h = h[:h.index("\n}")]
    flat = h.replace(" ", "").replace("\n", "")
    assert "method:'POST'" in flat, "_statusProbe no longer POSTs"
    assert "body:JSON.stringify({key:formKey})" in flat, "the key is not sent in the body"
    # the URL expression itself must not mention the key by any construction
    url_line = next(l for l in h.splitlines() if "/api/status/" in l)
    for leak in ("formKey", "key=", "URLSearchParams", "encodeURIComponent"):
        assert leak not in url_line, \
            f"_statusProbe puts {leak!r} in the request URL: {url_line.strip()!r}"

    # 3. nowhere in the file may a status URL carry a key
    assert "/api/status/${provider}?key" not in html
    for bad in ("key=${encodeURIComponent", "'?key='", '"?key="'):
        assert bad not in html, f"a status URL is still built with {bad!r}"


# ── crawl pause/cancel keying ────────────────────────────────────────────────
def test_pause_and_cancel_key_on_the_same_url_the_crawler_does():
    """The crawler strips the fragment from the seed before registering its gate, so a
    pause on 'https://x/#intro' looked up a key nobody had — and answered {'paused':true}
    while the crawl ran on at full speed."""
    src = (_SRC / "routes_ingestion.py").read_text()
    tree = ast.parse(src)
    for name in ("api_crawl_pause", "api_crawl_cancel", "api_ingest_url",
                 # the streaming twin is the endpoint the UI drives, and its own comment
                 # says a fragment here is what left pause/cancel keyed to nothing
                 "api_ingest_url_stream"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        assert "_strip_fragment" in ast.unparse(fn), f"{name} does not normalise the URL"
    assert crawler._strip_fragment("https://x/page#intro") == \
           crawler._strip_fragment("https://x/page")


def test_a_failed_retry_keeps_the_original_error_and_chains_the_second():
    """`raise e from None` discarded the retry error entirely: a 400 followed by a 429 on
    the retry surfaced only the 400, so the rate limit vanished with no trace anywhere."""
    providers._NO_STREAM_OPTIONS.discard("t_chain")
    calls = []

    class _C:
        async def create(self, **kw):
            calls.append(kw)
            raise _Boom("rate limit exceeded", 429) if len(calls) > 1 else _Boom("Bad Request", 400)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_C()))
    with pytest.raises(Exception) as ei:
        _drain(client, "t_chain")
    assert "Bad Request" in str(ei.value), "the original error is no longer surfaced"
    assert ei.value.__cause__ is not None, "the retry error was discarded"
    assert "rate limit" in str(ei.value.__cause__), "the retry error is not the chained cause"
    assert "t_chain" not in providers._NO_STREAM_OPTIONS


# ── RAG import must not store a vector of the wrong width ────────────────────
def test_import_re_embeds_a_vector_whose_width_does_not_match_the_index():
    """An exported vector carries the width of whatever model was active when it was
    written. Importing a 384-dim vector into a 768-dim index is silently rejected by
    RediSearch — every chunk present, nothing findable, which is exactly the symptom the
    field-name fix removed."""
    src = (_SRC / "routes_ingestion.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "api_import_rag")
    body = ast.unparse(fn)
    assert "get_sentence_embedding_dimension" in body, \
        "import does not read the loaded model's width (the registry reports 0 for a custom model)"
    assert "expect_bytes" in body and "len(emb_bytes) != expect_bytes" in body, \
        "import does not compare the stored vector's width against the index"

    # the arithmetic the guard depends on: float32 => 4 bytes per dimension
    for dims in (384, 768, 1024):
        assert len(np.zeros(dims, dtype=np.float32).tobytes()) == dims * 4


# ── crawl cancel must not orphan the embed worker ────────────────────────────
def test_crawl_teardown_propagates_cancellation_and_logs_failures():
    """The teardown caught `(CancelledError, Exception)` together and discarded both: a
    cancelled crawl reported success with task.cancelled() False, and an embed-worker
    failure vanished with no log line at all."""
    src = (_SRC / "crawler.py").read_text()
    tree = ast.parse(src)
    fin = next(ast.unparse(n.finalbody) for n in ast.walk(tree)
               if isinstance(n, ast.Try) and "queue.join()" in ast.unparse(n.body))
    assert "except (asyncio.CancelledError, Exception)" not in fin, \
        "cancellation and ordinary failure are still caught by one handler"
    assert "raise asyncio.CancelledError" in fin, "cancellation no longer propagates"
    assert "log.warning" in fin, "a failing embed worker is still discarded silently"
    assert "wait_for" in fin, "the drain is unbounded — a wedged worker hangs the teardown"


def test_endpoint_buttons_do_not_interpolate_a_name_into_a_js_string():
    """escHtml protects HTML text, not a JavaScript string literal: the browser decodes the
    attribute BEFORE compiling it, so an endpoint named  ');alert(1);//  escaped to
    &#x27;);alert(1);// and then executed. Reachable through an imported config."""
    html = (_SRC / "index.html").read_text(encoding="utf-8")
    for fn in ("deleteRedisEndpoint", "testRedisEndpoint"):
        assert f"{fn}('${{escHtml" not in html, \
            f"{fn} still builds a JS string from a user-supplied endpoint name"
    assert "data-ep-del" in html and "data-ep-test" in html, \
        "the endpoint buttons no longer pass the name as data"


def test_embed_worker_shutdown_is_in_the_finally_block():
    """Cancelling a crawl makes queue.join() raise, skipping everything after the
    try/finally — which left embed_worker's `while True` spinning for the life of the
    process, one orphan per cancelled crawl."""
    tree = ast.parse((_SRC / "crawler.py").read_text())
    joins = [n for n in ast.walk(tree)
             if isinstance(n, ast.Try)
             and "queue.join()" in ast.unparse(n.body)]
    assert joins, "the queue.join() try block moved — re-point this test"
    body = ast.unparse(joins[0].finalbody)
    assert "embed_task" in body, "embed worker shutdown is not in the finally block"
    assert "_newly_indexed" in body, "the indexed-URL flush is not in the finally block"


# ── RAG import/export must use the indexed vector field ──────────────────────
def test_rag_import_writes_the_field_the_index_was_built_against():
    """Import wrote the legacy flat 'embedding' while the index is built on the
    width-named field, so every imported chunk was stored outside the index — the
    instance held all its data and returned nothing from any search."""
    tree = ast.parse((_SRC / "routes_ingestion.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_import_rag")
    body = ast.unparse(fn)
    assert "vector_field_for()" in body, "import does not use the indexed vector field"
    assert "'embedding': emb_bytes" not in body and '"embedding": emb_bytes' not in body
    # The two spellings genuinely differ for every registered model, so writing the
    # literal really did put imported chunks outside the index. (vector_field_for() with
    # no argument falls back to the flat name when no model is loaded, as in this
    # process — so pin the assertion to the registry rather than the ambient state.)
    for spec in embeddings.EMBEDDING_MODELS.values():
        assert embeddings.vector_field_for(spec["repo"]) == f"embedding_{spec['dims']}"
        assert embeddings.vector_field_for(spec["repo"]) != "embedding"


class _FakeRedis:
    """Just enough Redis for the export generator: a keyspace scan and a pipeline of
    HGETALLs. Real enough to prove the reader, cheap enough to run offline."""

    def __init__(self, hashes: dict):
        self._h = hashes

    def scan_iter(self, match, count=None):
        yield from list(self._h)

    def pipeline(self, transaction=False):
        outer = self

        class _P:
            def __init__(self): self.keys = []
            def hgetall(self, k): self.keys.append(k)
            def execute(self): return [outer._h.get(k, {}) for k in self.keys]

        return _P()


@pytest.fixture
def width_named_field(monkeypatch):
    """Force the width-named vector field.

    With no model loaded, vector_field_for() falls back to the flat "embedding" — the same
    spelling as the legacy field — so the two code paths become indistinguishable and a
    test written against ambient state silently stops discriminating. Pin it.
    """
    monkeypatch.setattr(embeddings, "vector_field_for", lambda repo=None: "embedding_384")
    assert embeddings.vector_field_for() != "embedding"
    return b"embedding_384"


def test_export_emits_a_vector_for_every_chunk_including_the_last_partial_batch(width_named_field):
    """Full batches read the width-named field while the remainder read the legacy one, so
    the final partial batch of every export came out with EMPTY vectors — silently, and
    only for instances whose chunk count was not a multiple of the batch size.

    Asserted on the generator's real output. The previous version asserted
    `body.count("vector_field_for()") <= 1`, which 0 also satisfies — it was green with
    the original bug restored AND with the vectors dropped entirely.
    """
    field = width_named_field
    n = routes_ingestion._EXPORT_BATCH + 3          # forces a full batch plus a remainder
    vec = b"\x00\x01\x02\x03"
    hashes = {
        f"rag:x:chunk:{i}".encode(): {
            b"chunk_id": str(i).encode(), b"text": b"t", b"source": b"s", field: vec}
        for i in range(n)
    }
    out = list(routes_ingestion._iter_chunks_pipelined(_FakeRedis(hashes), "rag:x"))
    assert len(out) == n, f"expected {n} chunks, got {len(out)}"
    empty = [c["id"] for c in out if not c["embedding_b64"]]
    assert not empty, f"{len(empty)} chunk(s) exported with no vector, e.g. {empty[:5]}"
    # and specifically the remainder — the batch the old code read from the wrong field
    tail = out[routes_ingestion._EXPORT_BATCH:]
    assert tail and all(c["embedding_b64"] for c in tail), \
        "the final partial batch exported empty vectors"


def test_export_still_reads_a_legacy_pre_rename_vector(width_named_field):
    """Rows written before the field rename carry the flat b"embedding"; they must keep
    exporting rather than silently losing their vectors."""
    hashes = {b"rag:x:chunk:0": {b"chunk_id": b"0", b"text": b"t", b"source": b"s",
                                 b"embedding": b"\x00\x01\x02\x03"}}
    out = list(routes_ingestion._iter_chunks_pipelined(_FakeRedis(hashes), "rag:x"))
    assert len(out) == 1 and out[0]["embedding_b64"], "a legacy row exported with no vector"
