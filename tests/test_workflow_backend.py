# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server side of the crawl-observability, ingest-cancel and citation fixes.

These are behavioural, not structural: the ingest job is driven through the real route
with a stubbed indexer so the generator's own control flow decides the outcome, and the
crawler's frontier counters are read out of a real (tiny, local) crawl rather than
asserted against the source.
"""
import asyncio
import json
import pathlib
import re
import shutil
import tempfile

import pytest

from conftest import KEY_PREFIX

from redirecall import constants


# ── citation numbering ───────────────────────────────────────────────────────

def test_number_chunks_stamps_the_position_the_prompt_will_show(app_module):
    from redirecall import rag
    chunks = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    assert [c["n"] for c in rag.number_chunks(chunks)] == [1, 2, 3]


def test_the_prompt_numbers_match_the_stamp_not_the_list_position(app_module):
    """The stamp is the single source of truth for a citation number. If the prompt
    re-derived it from enumerate() the two could drift apart the moment anything reordered
    the list between stamping and rendering — which is exactly what the browser was doing.
    """
    from redirecall import rag
    chunks = rag.number_chunks([{"text": "first"}, {"text": "second"}])
    chunks.reverse()                       # a reorder after numbering
    prompt = rag.build_context_prompt(chunks)
    # Header line then body, per chunk. "second" is now listed first but must keep [2].
    pairs = [(m.group(1), m.group(2))
             for m in re.finditer(r"^\[(\d+)\][^\n]*\n(\w+)$", prompt, re.M)]
    assert pairs == [("2", "second"), ("1", "first")], (pairs, prompt)


def test_a_stored_turn_carries_the_citation_number(app_module):
    """Without it a reopened conversation cannot line its [n] markers up with the
    inspector: the projection in _turn_meta drops every field it does not name."""
    from redirecall import sessions
    meta = sessions._turn_meta(chunks=[{"text": "x", "n": 7}, {"text": "y", "n": 8}])
    assert [c["n"] for c in meta["chunks"]] == [7, 8]


def test_a_turn_stored_before_the_stamp_existed_still_gets_numbers(app_module):
    """Sessions already in Redis have no `n`; falling back to the position keeps the
    inspector labelling those turns rather than rendering "#undefined"."""
    from redirecall import sessions
    meta = sessions._turn_meta(chunks=[{"text": "x"}, {"text": "y"}])
    assert [c["n"] for c in meta["chunks"]] == [1, 2]


# ── provider status: "never set up" vs "set up and failing" ──────────────────

@pytest.mark.parametrize("provider", ["claude", "openai", "qwen", "mistral", "groq", "gemini"])
def test_a_keyless_provider_reports_itself_unconfigured(app_module, cfg, provider):
    """The only signal used to be the error PROSE, so the UI had to string-match
    "No API key configured" to tell a provider nobody set up from one that is broken."""
    from fastapi.testclient import TestClient
    from redirecall import appcore

    cfg.setdefault(provider, {})["api_key"] = ""
    with TestClient(appcore.app) as client:
        body = client.get(f"/api/status/{provider}").json()
    assert body["ok"] is False
    assert body["configured"] is False, body


# ── file ingest: observable and stoppable ────────────────────────────────────

def test_reap_finished_ingests_keeps_recent_outcomes_and_drops_the_rest(app_module):
    """Finished jobs are kept on purpose — a browser that reconnects after the stream ended
    still needs to learn how it ended — but keeping every one for the life of the process
    is the leak the crawl equivalent has."""
    from redirecall import state
    state._active_ingests.clear()
    for i in range(state._INGEST_HISTORY + 5):
        state._active_ingests[f"j{i}"] = {"job": f"j{i}", "done": True}
    state._active_ingests["live"] = {"job": "live", "done": False}

    dropped = state.reap_finished_ingests()
    assert dropped == 5
    assert "live" in state._active_ingests, "a running job was reaped"
    assert len([v for v in state._active_ingests.values() if v["done"]]) == state._INGEST_HISTORY
    assert "j0" not in state._active_ingests and "j24" in state._active_ingests
    state._active_ingests.clear()


def _sse_events(text: str) -> list[dict]:
    return [json.loads(ln[5:].strip()) for ln in text.splitlines() if ln.startswith("data:")]


@pytest.fixture
def stub_indexer(monkeypatch, app_module):
    """Replace the real indexer so the route's own control flow is what the test measures.
    Records which files it was actually asked to index."""
    from redirecall import ingest, rag_admin
    seen: list[str] = []

    async def fake_ingest_file(instance, path, name, rc):
        seen.append(name)
        await asyncio.sleep(0)
        return {"status": "ok", "chunks": 3}

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda instance, endpoint=None: None)
    return seen


def test_an_ingest_announces_a_job_id_and_appears_in_the_active_list(app_module, stub_indexer):
    """Both are what make a running ingest addressable at all: without the id there is
    nothing to cancel, and without the listing a reconnecting browser cannot find it."""
    from fastapi.testclient import TestClient
    from redirecall import appcore, state

    state._active_ingests.clear()
    inst = f"{KEY_PREFIX}ingest"
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{inst}/ingest/files/stream",
                        files=[("files", ("a.txt", b"hello", "text/plain")),
                               ("files", ("b.txt", b"world", "text/plain"))])
        events = _sse_events(r.text)

    assert events[0].get("job"), events[:2]
    job = events[0]["job"]
    assert events[0]["total"] == 2
    assert events[-1]["done"] is True and events[-1]["cancelled"] is False
    assert stub_indexer == ["a.txt", "b.txt"]

    listed = {j["job"]: j for j in state._active_ingests.values()}
    assert job in listed and listed[job]["done"] is True
    assert listed[job]["ok"] == 2
    state._active_ingests.clear()


def test_cancelling_stops_before_the_next_file_and_keeps_the_ones_already_done(
        app_module, monkeypatch):
    """A cancel between items, not a rollback. The file being indexed when the cancel
    lands finishes; the ones after it are never started, and their uploads are removed
    rather than left in the uploads directory for good.
    """
    from fastapi.testclient import TestClient
    from redirecall import appcore, ingest, rag_admin, state

    state._active_ingests.clear()
    seen: list[str] = []

    async def fake_ingest_file(instance, path, name, rc):
        seen.append(name)
        if name == "b.txt":                      # cancel arrives while b is indexing
            job = next(iter(state._ingest_cancels))
            state._ingest_cancels[job].set()
        await asyncio.sleep(0)
        return {"status": "ok", "chunks": 1}

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda instance, endpoint=None: None)

    inst = f"{KEY_PREFIX}ingest2"
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{inst}/ingest/files/stream",
                        files=[("files", (n, b"x", "text/plain"))
                               for n in ("a.txt", "b.txt", "c.txt", "d.txt")])
        events = _sse_events(r.text)

    assert seen == ["a.txt", "b.txt"], "files after the cancel were still indexed"
    final = events[-1]
    assert final["done"] is True and final["cancelled"] is True, final
    assert final["ok"] == 2
    # The docstring's other claim, now actually checked: uploads never reached must not be
    # left behind. Nothing asserted this, so the outer finally that removes them was
    # untested — and it is the only thing standing between a cancel and a growing
    # uploads directory.
    left = [p for p in constants.UPLOAD_DIR.glob("*.txt")
            if p.name in ("a.txt", "b.txt", "c.txt", "d.txt")]
    assert not left, f"cancelled uploads left on disk: {[p.name for p in left]}"
    state._active_ingests.clear()


def test_cancelling_an_ingest_that_is_not_running_is_a_404_not_a_silent_ok(app_module):
    """Reporting success for a job nobody can find tells the UI the crawl stopped when
    nothing happened — the same lie /api/crawl/pause used to tell."""
    from fastapi.testclient import TestClient
    from redirecall import appcore
    with TestClient(appcore.app) as client:
        assert client.post("/api/ingest/cancel", json={"job": "nope"}).status_code == 404


def test_the_active_ingest_route_reports_what_a_real_running_job_looks_like(
        app_module, monkeypatch):
    """Read from a job the PRODUCTION loop is running, not from a dict the test wrote.

    Its predecessor inserted its own record and asserted the route echoed it — which only
    proves a dict survives JSON serialisation, and would have passed just as well while the
    loop never set `current`, `index`, `ok` or `errors` at all.
    """
    from fastapi.testclient import TestClient
    from redirecall import appcore, ingest, rag_admin, state

    state._active_ingests.clear()
    snapshots: list = []

    async def fake_ingest_file(instance, path, name, rc):
        # Look at the API mid-run, which is the only moment the fields mean anything.
        with TestClient(appcore.app) as c2:
            snapshots.append(c2.get("/api/ingest/active").json())
        await asyncio.sleep(0)
        return {"status": "ok", "chunks": 2}

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda instance, endpoint=None: None)

    inst = f"{KEY_PREFIX}live"
    with TestClient(appcore.app) as client:
        client.post(f"/api/rag/{inst}/ingest/files/stream",
                    files=[("files", (n, b"x", "text/plain")) for n in ("p.txt", "q.txt")])

    mid = [row for snap in snapshots for row in snap if row["instance"] == inst]
    assert mid, f"a running ingest was not listed: {snapshots}"
    assert mid[0]["current"] == "p.txt", mid[0]
    assert mid[0]["total"] == 2 and mid[0]["done"] is False
    assert mid[-1]["current"] == "q.txt" and mid[-1]["index"] == 1, mid[-1]
    assert mid[-1]["ok"] == 1, "the running tally did not advance"
    # Server-side bookkeeping must not be published.
    assert "upload_paths" not in mid[0] and "registered_at" not in mid[0], mid[0]
    state._active_ingests.clear()


def test_a_file_that_fails_to_index_is_counted_as_an_error_not_a_success(
        app_module, monkeypatch):
    """ingest_file REPORTS failure by returning {"status": "error"} — an unsupported type,
    a scanned PDF with no extractable text — it does not raise. Counting successes on the
    non-exception path therefore scored every one of those as `ok`, so a batch in which
    nothing was indexed reported ok=N, errors=0 to both the stream and /api/ingest/active.
    """
    import json as _json
    from fastapi.testclient import TestClient
    from redirecall import appcore, ingest, rag_admin, state

    state._active_ingests.clear()

    async def fake_ingest_file(instance, path, name, rc):
        await asyncio.sleep(0)
        if name == "good.txt":
            return {"status": "ok", "chunks": 3}
        if name == "scan.pdf":
            return {"status": "error", "error": "No extractable text", "chunks": 0}
        return {"status": "skipped", "error": "Unsupported type: .doc"}

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda instance, endpoint=None: None)

    inst = f"{KEY_PREFIX}mixed"
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{inst}/ingest/files/stream",
                        files=[("files", (n, b"x", "text/plain"))
                               for n in ("good.txt", "scan.pdf", "old.doc")])
        events = [_json.loads(ln[5:]) for ln in r.text.splitlines() if ln.startswith("data:")]

    final = events[-1]
    assert final["ok"] == 1, f"failures were counted as successes: {final}"
    assert final["errors"] == 2, final
    # the per-file events must carry the reason, not just a status word
    scan = next(e for e in events if e.get("file") == "scan.pdf")
    assert scan["status"] == "error" and "extractable" in scan["error"], scan
    state._active_ingests.clear()


def test_a_job_whose_stream_never_starts_is_expired_rather_than_left_immortal(app_module):
    """Registration happens before the response is returned; everything that clears a job
    lives in the generator's finally. A client that disconnects before pulling the first
    chunk therefore left a job registered, not done, and unreachable — and the UI attaches
    to the first not-done job it finds, so one phantom froze that panel for good.
    """
    from redirecall import state

    state._active_ingests.clear()
    state._ingest_cancels.clear()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rr-phantom-"))
    orphan = tmp / "never-indexed.txt"
    orphan.write_bytes(b"x")

    state._active_ingests["ghost"] = {
        "job": "ghost", "instance": "i", "total": 1, "index": 0, "ok": 0, "errors": 0,
        "started": False, "registered_at": 1000.0, "done": False, "cancelled": False,
        "upload_paths": [str(orphan)],
    }
    state._ingest_cancels["ghost"] = asyncio.Event()

    # Inside the grace window it must be left alone: a slow client is not a phantom.
    assert state.reap_finished_ingests(now=1000.0 + 5) == 0
    assert "ghost" in state._active_ingests
    assert orphan.exists()

    assert state.reap_finished_ingests(now=1000.0 + state._INGEST_START_GRACE + 1) == 1
    assert "ghost" not in state._active_ingests
    assert "ghost" not in state._ingest_cancels
    assert not orphan.exists(), "the upload it never indexed was left on disk"

    # A job that DID start is never expired by this path, however long it runs.
    state._active_ingests["longrun"] = {"job": "longrun", "started": True, "done": False,
                                        "registered_at": 0.0, "upload_paths": []}
    assert state.reap_finished_ingests(now=1e9) == 0
    assert "longrun" in state._active_ingests
    state._active_ingests.clear()
    shutil.rmtree(tmp, ignore_errors=True)


# ── crawl frontier counters ──────────────────────────────────────────────────

def test_a_crawl_reports_how_many_urls_it_has_discovered(app_module, monkeypatch, tmp_path):
    """pages_done alone gives a progress bar nothing to divide by when max_pages is 0 —
    the shipped default. discovered/queued come from the crawler's own frontier, so they
    cannot drift from what it is really doing.

    Driven through the real crawl_url with fetching, chunking and embedding stubbed: the
    BFS, the frontier bookkeeping and the worker loop are all the production code.
    """
    from redirecall import crawler, ingest, rag, rag_admin

    PAGES = {
        "https://seed.example/": "<a href='https://seed.example/a'>a</a>"
                                 "<a href='https://seed.example/b'>b</a>",
        "https://seed.example/a": "<a href='https://seed.example/c'>c</a> alpha text",
        "https://seed.example/b": "beta text",
        "https://seed.example/c": "gamma text",
    }

    class _FakeRedis:
        def smembers(self, *a, **k): return set()
        def delete(self, *a, **k): return 0
        def pipeline(self, **k): return self
        def sadd(self, *a, **k): return self
        def execute(self): return []

    monkeypatch.setattr(rag_admin, "rc_for_instance", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(crawler, "can_crawl", lambda url: True)
    monkeypatch.setattr(crawler, "assert_public_url", lambda url: None)
    monkeypatch.setattr(ingest, "_prepare_chunks",
                        lambda inst, text, src, rc, force: [{"text": text, "source": src}])
    monkeypatch.setattr(rag, "add_chunks", lambda inst, records, rc: len(records))

    # fetch_url is the seam: patching it keeps _extract_html_links, the BFS, the frontier
    # bookkeeping and the worker loop as production code, and only the socket is fake.
    async def fake_fetch(url):
        return PAGES.get(crawler._strip_fragment(url), "")

    monkeypatch.setattr(crawler, "fetch_url", fake_fetch)

    stats: dict = {}
    asyncio.run(crawler.crawl_url("t", "https://seed.example/", depth=2,
                                  respect_robots=False, stats=stats, smart_mode=False,
                                  rc=_FakeRedis(), concurrency=2))

    # Four distinct URLs enter the frontier; the crawl only ends once it has drained.
    assert stats["discovered"] == len(PAGES), stats
    assert stats["queued"] == 0, stats


def test_a_crawl_given_no_stats_dict_still_runs(app_module, monkeypatch):
    """`stats` is optional — the scheduled re-crawl and the non-streaming route call
    crawl_url without one, and a None there must not become an AttributeError mid-crawl."""
    from redirecall import crawler, ingest, rag, rag_admin

    class _FakeRedis:
        def smembers(self, *a, **k): return set()
        def delete(self, *a, **k): return 0
        def pipeline(self, **k): return self
        def sadd(self, *a, **k): return self
        def execute(self): return []

    monkeypatch.setattr(rag_admin, "rc_for_instance", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(crawler, "can_crawl", lambda url: True)
    monkeypatch.setattr(crawler, "assert_public_url", lambda url: None)
    monkeypatch.setattr(ingest, "_prepare_chunks",
                        lambda inst, text, src, rc, force: [{"text": text, "source": src}])
    monkeypatch.setattr(rag, "add_chunks", lambda inst, records, rc: len(records))

    async def fake_fetch(url):
        return "<p>only text</p>"

    monkeypatch.setattr(crawler, "fetch_url", fake_fetch)
    indexed: list = []

    async def cb(u, status, n=0, err="", count=0):
        indexed.append((u, status))

    # smart_mode=False explicitly: with crawl4ai installed the default routes this through
    # a real headless browser, which is not what this test is about.
    asyncio.run(crawler.crawl_url("t", "https://only.example/", depth=0,
                                  respect_robots=False, rc=_FakeRedis(),
                                  smart_mode=False, progress_cb=cb))
    # Crash-only is not a test: without this the body could be `pass` and still "pass".
    assert ("https://only.example/", "indexed") in indexed, indexed


def test_the_crawl_route_publishes_the_crawlers_own_frontier_to_the_api(app_module, monkeypatch):
    """The seam between the crawler and the API, driven end to end.

    Its predecessor asserted that three field names appeared in the route's SOURCE. That
    passes if the names sit in a comment, and — decisively — it passes with the
    `stats=...` argument deleted outright, because the dict literal still declares the
    keys. What has to be true is that the numbers on /api/crawl/active are the CRAWLER's,
    so the numbers here are ones only the crawler can produce.
    """
    import json as _json
    from fastapi.testclient import TestClient
    from redirecall import appcore, crawler, rag_admin, state

    PAGES = {
        "https://seam.example/": "<a href='https://seam.example/a'>a</a>"
                                 "<a href='https://seam.example/b'>b</a> hello",
        "https://seam.example/a": "alpha",
        "https://seam.example/b": "beta",
    }
    seen_api: list = []

    async def fake_crawl(instance, url, depth=0, **kw):
        stats = kw.get("stats")
        assert stats is not None, "the route did not hand the crawler a stats dict"
        # Stand in for the BFS: admit three URLs, resolve two, and leave one queued.
        stats["discovered"], stats["queued"], stats["resolved"] = 3, 1, 2
        stats["pages_done"] = 2
        cb = kw.get("progress_cb")
        if cb:
            await cb("https://seam.example/a", "indexed", 4, "", 2)
        # Read the API back WHILE the crawl is running — after it ends the entry is
        # marked done and a stale snapshot would look the same.
        with TestClient(appcore.app) as c2:
            seen_api.extend(c2.get("/api/crawl/active").json())

    monkeypatch.setattr(crawler, "crawl_url", fake_crawl)
    monkeypatch.setattr(crawler, "assert_public_url", lambda u: None)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda instance, endpoint=None: None)
    state._active_crawls.clear()

    inst = f"{KEY_PREFIX}seam"
    with TestClient(appcore.app) as client:
        r = client.get(f"/api/rag/{inst}/ingest/url/stream",
                       params={"url": "https://seam.example/", "depth": 1,
                               "max_pages": 0, "smart_mode": False})
        events = [_json.loads(ln[5:]) for ln in r.text.splitlines() if ln.startswith("data:")]

    mine = [c for c in seen_api if c["url"] == "https://seam.example/"]
    assert mine, f"the running crawl was not listed: {seen_api}"
    assert (mine[0]["discovered"], mine[0]["queued"], mine[0]["resolved"]) == (3, 1, 2), mine[0]
    assert mine[0]["max_pages"] == 0

    # ...and the same numbers ride the SSE stream the UI reads.
    page = [e for e in events if e.get("status") == "indexed"]
    assert page and page[0]["discovered"] == 3 and page[0]["queued"] == 1, page
    state._active_crawls.clear()


def test_the_chat_route_stamps_citation_numbers_before_it_builds_the_prompt(
        app_module, monkeypatch, cfg):
    """Guards the CALL SITE, not the helper.

    `rag.number_chunks` has its own tests, but deleting the line that calls it from the
    chat path failed nothing: the prompt and the stored turn both fall back to enumerate
    position and look correct, so the numbers only diverge later, when something reorders
    the payload. What matters is that the chunks are already stamped by the time the
    prompt is rendered — which is what this reads, from the list the prompt builder is
    actually handed.
    """
    from fastapi.testclient import TestClient
    from redirecall import appcore, cache, providers, rag, sessions, state

    seen: dict = {}
    real_build = rag.build_context_prompt

    def spy_build(chunks):
        seen["chunks"] = [dict(c) for c in chunks]
        return real_build(chunks)

    # Deliberately NOT in relevance order, and with no `n`: the route must supply it.
    HITS = [{"text": "beta", "source": "b.md", "relevance": 0.4, "instance": "i"},
            {"text": "alpha", "source": "a.md", "relevance": 0.9, "instance": "i"}]

    def fake_search(instance, query, k, *a, **kw):
        return [dict(c) for c in HITS]

    async def fake_stream(*a, **kw):
        for tok in ("grounded ", "answer [2]"):
            yield tok, False
        yield "", True

    monkeypatch.setattr(rag, "build_context_prompt", spy_build)
    # The single-instance branch is the one a default install takes.
    monkeypatch.setattr(rag, "search_rag", fake_search)
    monkeypatch.setattr(providers, "ollama_stream", fake_stream)
    monkeypatch.setattr(cache, "cache_lookup", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "save_session", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(app_module.embeddings, "rerank_chunks",
                        lambda q, chunks, top_n=5: chunks)

    cfg["active_rag"] = "default"
    sid = f"{KEY_PREFIX}chat"
    with TestClient(appcore.app) as client:
        r = client.post("/api/chat", json={"message": "what about streams?",
                                           "session_id": sid, "provider": "ollama",
                                           "model": "llama3", "use_cache": False})
    assert r.status_code == 200, r.text
    assert seen.get("chunks"), "the prompt builder was never reached"
    assert [c["n"] for c in seen["chunks"]] == [1, 2], seen["chunks"]

    # ...and the numbers that reach the client are the same ones.
    body = r.json()
    assert [c["n"] for c in body["chunks"]] == [1, 2], body["chunks"]
    state._sessions.pop(sid, None)


def test_the_streaming_upload_route_enforces_the_same_size_cap_as_its_twin(
        app_module, monkeypatch, stub_indexer):
    """`_MAX_UPLOAD_BYTES` was checked only in the non-streaming route, and the UI drives
    the streaming one exclusively — so the limit did not apply on the one path anybody
    takes. Also checks that a rejected batch does not leave its predecessors behind.
    """
    from fastapi.testclient import TestClient
    from redirecall import appcore, config, constants

    monkeypatch.setattr(config, "_MAX_UPLOAD_BYTES", 64)
    inst = f"{KEY_PREFIX}big"
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{inst}/ingest/files/stream",
                        files=[("files", ("small.txt", b"a" * 10, "text/plain")),
                               ("files", ("huge.txt", b"b" * 500, "text/plain"))])
    assert r.status_code == 413, (r.status_code, r.text[:200])
    assert "huge.txt" in r.text
    assert not (constants.UPLOAD_DIR / "small.txt").exists(), \
        "the accepted file was left on disk after the batch was rejected"
    assert stub_indexer == [], "a rejected batch must not index anything"


def test_a_crawl_counts_the_pages_it_has_resolved(app_module, monkeypatch):
    """`resolved` is what the progress bar divides by, so it has to be the crawler's own
    tally: a URL is admitted to the frontier before the crawlable-type, already-indexed,
    robots and duplicate checks, any of which end it without an index. Summing it from
    counters the CALLER keeps meant a caller that keeps none silently reported zero — and
    that it could not be asserted here at all.

    Two of the four pages below never index: one is already in the skip-list and one is a
    binary type. Both must still count as resolved, or the bar can never fill.
    """
    from redirecall import crawler, ingest, rag, rag_admin

    PAGES = {
        "https://res.example/": "<a href='https://res.example/a'>a</a>"
                                "<a href='https://res.example/old'>old</a>"
                                "<a href='https://res.example/f.zip'>z</a> seed text",
        "https://res.example/a": "alpha text",
        "https://res.example/old": "already indexed text",
    }

    class _FakeRedis:
        def smembers(self, *a, **k): return {b"https://res.example/old"}   # the skip-list
        def delete(self, *a, **k): return 0
        def pipeline(self, **k): return self
        def sadd(self, *a, **k): return self
        def execute(self): return []

    async def fake_fetch(url):
        return PAGES.get(crawler._strip_fragment(url), "")

    monkeypatch.setattr(rag_admin, "rc_for_instance", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(crawler, "fetch_url", fake_fetch)
    monkeypatch.setattr(crawler, "assert_public_url", lambda u: None)
    monkeypatch.setattr(ingest, "_prepare_chunks",
                        lambda inst, text, src, rc, force: [{"text": text, "source": src}])
    monkeypatch.setattr(rag, "add_chunks", lambda inst, records, rc: len(records))

    seen: list = []

    async def cb(u, status, n=0, err="", count=0):
        seen.append(status)

    stats: dict = {}
    asyncio.run(crawler.crawl_url("t", "https://res.example/", depth=2,
                                  respect_robots=False, stats=stats, smart_mode=False,
                                  rc=_FakeRedis(), concurrency=2, progress_cb=cb))

    terminal = [x for x in seen if x in ("indexed", "skipped", "blocked", "error")]
    assert stats["resolved"] == len(terminal), (stats, seen)
    # Indexed alone cannot reach discovered — that is the whole point.
    indexed = seen.count("indexed")
    assert indexed < stats["resolved"], (indexed, stats)
    assert stats["resolved"] == stats["discovered"], \
        f"the crawl drained but resolved {stats['resolved']} of {stats['discovered']}"
    assert stats["queued"] == 0, stats


def test_the_websocket_chat_path_also_stamps_citation_numbers(app_module, monkeypatch, cfg):
    """The other call site. The REST test above covers only its own branch, so with just
    this one removed nothing failed — and the WebSocket path is the one the browser uses
    for every ordinary turn, which makes it the one that matters most.

    Driven by calling handle_chat with a stand-in socket: the transport is not what is
    under test, the payload it is handed is.
    """
    from redirecall import cache, providers, rag, routes_chat, sessions, state
    from redirecall import ws as _ns_ws

    sent: list = []

    class _FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    HITS = [{"text": "beta", "source": "b.md", "relevance": 0.4, "instance": "i"},
            {"text": "alpha", "source": "a.md", "relevance": 0.9, "instance": "i"}]

    def fake_search(instance, query, k, *a, **kw):
        return [dict(c) for c in HITS]

    async def fake_stream(*a, **kw):
        for tok in ("grounded ", "answer [2]"):
            yield tok, False
        yield "", True

    monkeypatch.setattr(rag, "search_rag", fake_search)
    monkeypatch.setattr(providers, "ollama_stream", fake_stream)
    monkeypatch.setattr(cache, "cache_lookup", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "save_session", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(app_module.embeddings, "rerank_chunks",
                        lambda q, chunks, top_n=5: chunks)
    monkeypatch.setattr(_ns_ws.mgr, "is_aborted", lambda sid: False, raising=False)

    cfg["active_rag"] = "default"
    sid = f"{KEY_PREFIX}wschat"
    state._sessions[sid] = []
    asyncio.run(routes_chat.handle_chat(_FakeWS(), sid, {
        "content": "what about streams?", "provider": "ollama", "model": "llama3",
        "bypass_cache": True,
    }))

    ctx = [m for m in sent if m.get("type") == "rag_context"]
    assert ctx, f"no rag_context was sent: {[m.get('type') for m in sent]}"
    assert [c["n"] for c in ctx[0]["chunks"]] == [1, 2], ctx[0]["chunks"]
    state._sessions.pop(sid, None)


# ── keeping an answer: the text-ingest route ─────────────────────────────────

@pytest.fixture
def app_on_test_redis(cfg, clean_redis):
    """Point the app's own client at the test server for the duration of one test.

    The suite's Redis is not the app's default (127.0.0.1:6390 vs localhost:6379), so a
    route-level test that really stores and retrieves has to move the app onto it — and
    move it back, or every later test inherits the override.
    """
    from conftest import REDIS_DB, REDIS_HOST, REDIS_PORT
    from redirecall import redis_store
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB,
                    "password": "", "ssl": False}
    redis_store.invalidate_redis_clients()
    yield clean_redis
    redis_store.invalidate_redis_clients()


def test_saving_text_makes_it_a_findable_deletable_document(app_module, app_on_test_redis):
    """The whole round trip against a real Redis, because each leg is what makes the
    feature usable rather than just written: the text has to become chunks, the chunks
    have to be retrievable by a question that never quotes them verbatim, the document has
    to appear under its own name, and it has to be removable on its own afterwards.

    Nothing else in the app could store an answer: the semantic cache expires on
    cache.ttl, the conversation on sessions.ttl, and the pin is browser-session state.

    The route handlers are called directly rather than through TestClient because the
    app's lifespan startup reloads config from disk and would undo the redirection onto
    the test server. The code under test is the same either way — only the HTTP layer,
    which these assertions say nothing about, is skipped.
    """
    from redirecall import rag, routes_ingestion, routes_sources

    inst = f"{KEY_PREFIX}kept"
    source = "answer://2026-08-19 how the semantic cache expires"
    text = ("Q: how long does a cached answer live?\n\n"
            "A: The semantic cache holds an entry for cache.ttl seconds, one hour by "
            "default, after which it is evicted and the question is answered afresh.")

    saved = asyncio.run(routes_ingestion.api_ingest_text(inst, {"text": text, "source": source}))
    assert saved["chunks"] >= 1 and saved["duplicate"] is False, saved

    # ...it is a document in its own right, not an anonymous blob
    docs = routes_sources.api_rag_documents(inst)["documents"]
    mine = [d for d in docs if d.get("source") == source]
    assert mine, f"the saved answer is not listed as a document: {docs}"
    assert mine[0]["chunks"] == saved["chunks"]

    # ...and retrievable by a question phrased differently from the text
    hits = rag.search_rag(inst, "when does a cached response get dropped?",
                          5, 0.0, app_on_test_redis)
    assert any(h["source"] == source for h in hits), \
        f"the saved answer was not retrievable: {[h.get('source') for h in hits]}"

    # ...re-saving the same thing under the same name stores nothing twice
    again = asyncio.run(routes_ingestion.api_ingest_text(inst, {"text": text, "source": source}))
    assert again["chunks"] == 0 and again["duplicate"] is True, again

    # ...and it can be taken back out on its own
    routes_sources.api_delete_document(inst, source)
    left = [x for x in routes_sources.api_rag_documents(inst)["documents"]
            if x.get("source") == source]
    assert not left, f"the document survived its own delete: {left}"


@pytest.mark.parametrize("payload,status,why", [
    ({"text": "", "source": "answer://x"},   400, "empty text"),
    ({"text": "some answer", "source": ""},  400, "empty source"),
    ({"text": "some answer"},                400, "missing source"),
    ({"text": "some answer", "source": "a" * 401}, 400, "over-long source"),
])
def test_a_saved_answer_must_be_attributable(app_module, payload, status, why):
    """A chunk with no source is unattributable AND undeletable — the per-document delete
    matches on exactly that value, so there would be no way to take it back out short of
    resetting the whole instance."""
    from fastapi.testclient import TestClient
    from redirecall import appcore
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{KEY_PREFIX}bad/ingest/text", json=payload)
    assert r.status_code == status, (why, r.status_code, r.text[:200])


def test_saving_text_is_bounded_by_the_upload_limit(app_module, monkeypatch):
    """Same ceiling as the file and streaming upload routes: this one takes its body from
    a JSON field, which is no reason for it to be the one route without a cap."""
    from fastapi.testclient import TestClient
    from redirecall import appcore, config
    monkeypatch.setattr(config, "_MAX_UPLOAD_BYTES", 64)
    with TestClient(appcore.app) as client:
        r = client.post(f"/api/rag/{KEY_PREFIX}big/ingest/text",
                        json={"text": "x" * 500, "source": "answer://big"})
    assert r.status_code == 413, (r.status_code, r.text[:200])
