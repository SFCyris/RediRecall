# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provider-reported token usage: the sink contract, the shared OpenAI-SDK loop
(usage chunk arrives AFTER finish_reason), the turn-meta plumbing, and the
conversation fork endpoint. All offline — providers are exercised with fake
SDK objects, never a network call."""
import asyncio
import os
import types

import pytest

from redirecall import providers, sessions, state, routes_misc


# ── the sink contract ────────────────────────────────────────────────────────
def test_sink_records_valid_counts_and_optional_cached():
    s: dict = {}
    providers._sink_usage(s, 120, 45, cached=30)
    assert s == {"prompt": 120, "completion": 45, "cached": 30}


@pytest.mark.parametrize("p,c", [(None, 5), (5, None), ("x", 5), (-1, 5), (5, -2)])
def test_sink_rejects_partial_or_invalid_counts(p, c):
    s: dict = {}
    providers._sink_usage(s, p, c)
    assert s == {}, f"partial record must not be written: {p!r},{c!r} -> {s}"


def test_sink_tolerates_none_sink():
    providers._sink_usage(None, 10, 10)   # must not raise


# ── the shared OpenAI-SDK loop ───────────────────────────────────────────────
def _chunk(content=None, finish=None, usage=None, x_groq_usage=None):
    delta = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish)
    return types.SimpleNamespace(
        choices=[choice] if (content is not None or finish) else [],
        usage=usage,
        x_groq=types.SimpleNamespace(usage=x_groq_usage) if x_groq_usage else None)


class _FakeStream:
    def __init__(self, chunks): self._chunks = chunks
    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _FakeClient:
    """chat.completions.create stub; optionally rejects stream_options."""
    def __init__(self, chunks, reject_stream_options=False):
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls.append(kw)
                if reject_stream_options and "stream_options" in kw:
                    raise RuntimeError("unknown parameter: stream_options")
                return _FakeStream(chunks)

        self.chat = types.SimpleNamespace(completions=_Completions())


def _run(coro):
    """Run on a fresh loop and CLOSE it — an unclosed loop leaks a kqueue fd per call."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_usage_chunk_after_finish_reason_is_captured():
    """The final usage chunk has EMPTY choices and arrives after finish_reason —
    the old loops broke on finish_reason and would have missed it."""
    u = types.SimpleNamespace(prompt_tokens=321, completion_tokens=64)
    client = _FakeClient([_chunk("Hel"), _chunk("lo"), _chunk(finish="stop"),
                          _chunk(usage=u)])
    sink: dict = {}

    async def collect():
        toks = []
        async for t, d in providers._openai_compat_stream(
                "t1", client, "m", [], sink, want_stream_options=True):
            toks.append(t)
        return toks

    assert _run(collect()) == ["Hel", "lo"]
    assert sink == {"prompt": 321, "completion": 64}


def test_groq_spelling_x_groq_usage_is_read():
    u = types.SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    client = _FakeClient([_chunk("ok"), _chunk(finish="stop", x_groq_usage=u)])
    sink: dict = {}

    async def collect():
        async for _ in providers._openai_compat_stream(
                "t2", client, "m", [], sink, want_stream_options=False):
            pass

    _run(collect())
    assert sink == {"prompt": 11, "completion": 7}


def test_stream_options_rejection_falls_back_and_is_remembered():
    providers._NO_STREAM_OPTIONS.discard("t3")
    client = _FakeClient([_chunk("x"), _chunk(finish="stop")], reject_stream_options=True)
    sink: dict = {}

    async def collect():
        async for _ in providers._openai_compat_stream(
                "t3", client, "m", [], sink, want_stream_options=True):
            pass

    _run(collect())
    # first call carried stream_options and failed; the retry did not
    assert "stream_options" in client.calls[0] and "stream_options" not in client.calls[1]
    assert "t3" in providers._NO_STREAM_OPTIONS
    # a second stream skips the doomed attempt entirely
    client2 = _FakeClient([_chunk("y"), _chunk(finish="stop")], reject_stream_options=True)
    _run(_collect_all(client2, "t3"))
    assert len(client2.calls) == 1 and "stream_options" not in client2.calls[0]
    providers._NO_STREAM_OPTIONS.discard("t3")


async def _collect_all(client, key):
    async for _ in providers._openai_compat_stream(key, client, "m", [], {},
                                                   want_stream_options=True):
        pass


# ── turn meta ────────────────────────────────────────────────────────────────
def test_turn_meta_includes_usage_only_when_complete():
    m = sessions._turn_meta(provider="openai", model="gpt-4o",
                            usage={"prompt": 10, "completion": 3})
    assert m["usage"] == {"prompt": 10, "completion": 3}
    m2 = sessions._turn_meta(provider="openai", model="gpt-4o", usage={})
    assert "usage" not in m2


# ── cumulative tally (namespaced test key — never the real one) ──────────────
def test_record_usage_and_totals_roundtrip(monkeypatch):
    key = f"__rrtest_{os.getpid()}__:usage"
    monkeypatch.setattr(sessions, "_USAGE_KEY", key)
    import redis as _redis
    from conftest import REDIS_HOST, REDIS_PORT
    from redirecall import redis_store
    try:
        rc = _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=2)
        rc.ping()
        rc.delete(key)
    except Exception:
        pytest.skip("redis not reachable")
    monkeypatch.setattr(redis_store, "r", lambda: rc)
    sessions.record_usage("openai", "gpt-4o", {"prompt": 100, "completion": 40})
    sessions.record_usage("openai", "gpt-4o", {"prompt": 50, "completion": 10, "cached": 5})
    totals = sessions.usage_totals()
    assert totals["openai:gpt-4o"]["in"] == 150
    assert totals["openai:gpt-4o"]["out"] == 50
    assert totals["openai:gpt-4o"]["cached"] == 5
    rc.delete(key)


@pytest.fixture
def usage_redis(monkeypatch):
    """A namespaced tally key on the real Redis, or skip. Never touches the live key.

    A fixture rather than a helper so the client is actually CLOSED afterwards — a bare
    redis.Redis() per test leaks a socket, and this project has already had one
    FD-exhaustion incident.
    """
    key = f"__rrtest_{os.getpid()}__:usage"
    monkeypatch.setattr(sessions, "_USAGE_KEY", key)
    import redis as _redis
    from conftest import REDIS_HOST, REDIS_PORT, REDIS_DB
    from redirecall import redis_store
    try:
        rc = _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                          socket_connect_timeout=2)
        rc.ping()
        rc.delete(key)
    except Exception:
        pytest.skip("redis not reachable")
    monkeypatch.setattr(redis_store, "r", lambda: rc)
    try:
        yield rc, key
    finally:
        try:
            rc.delete(key)
        finally:
            rc.close()


def test_cache_write_tokens_reach_the_cumulative_tally(usage_redis):
    """Claude bills cache CREATION at ~1.25x input, separately from cache reads. The
    per-turn meta carried it from the start but the cumulative tally silently dropped it,
    so the all-time cost was under-reported by the most expensive token class there is."""
    rc, key = usage_redis
    sessions.record_usage("claude", "sonnet",
                          {"prompt": 10, "completion": 2, "cached": 4, "cache_write": 7})
    sessions.record_usage("claude", "sonnet",
                          {"prompt": 1, "completion": 1, "cache_write": 3})
    t = sessions.usage_totals()["claude:sonnet"]
    assert t["cache_write"] == 10, t
    assert t == {"in": 11, "out": 3, "cached": 4, "cache_write": 10}


def test_a_model_name_containing_colons_round_trips(usage_redis):
    """Fields are '<provider>:<model>:<kind>' and read back by splitting on the LAST
    colon. Ollama tags and OpenRouter ids carry colons of their own, so a naive split
    would truncate the model and merge two models' counters into one row."""
    rc, key = usage_redis
    sessions.record_usage("ollama", "qwen2.5:7b", {"prompt": 5, "completion": 1})
    sessions.record_usage("ollama", "qwen2.5:14b", {"prompt": 9, "completion": 2})
    t = sessions.usage_totals()
    assert t["ollama:qwen2.5:7b"]["in"] == 5
    assert t["ollama:qwen2.5:14b"]["in"] == 9


def test_clear_usage_zeroes_the_tally_and_survives_a_second_call(usage_redis):
    rc, key = usage_redis
    sessions.record_usage("openai", "gpt-4o", {"prompt": 100, "completion": 40})
    assert sessions.usage_totals()
    sessions.clear_usage()
    assert sessions.usage_totals() == {}
    sessions.clear_usage()          # clearing an already-empty tally must not raise
    assert sessions.usage_totals() == {}
    # and the counter still works afterwards — delete must not leave a broken key
    sessions.record_usage("openai", "gpt-4o", {"prompt": 7, "completion": 1})
    assert sessions.usage_totals()["openai:gpt-4o"]["in"] == 7


def test_usage_endpoints_read_and_clear(usage_redis):
    """The two routes the Analytics token table drives — exercised through the HTTP
    router, not by calling the handlers.

    Calling the function objects proved nothing about routing: deleting the
    @app.delete("/api/usage") decorator outright, or moving it to
    GET /api/usage/clear, both left this green while the Reset button 404s.
    """
    from fastapi.testclient import TestClient
    from redirecall import appcore
    rc, key = usage_redis
    client = TestClient(appcore.app)
    sessions.record_usage("openai", "gpt-4o", {"prompt": 20, "completion": 5})

    r = client.get("/api/usage")
    assert r.status_code == 200, f"GET /api/usage -> {r.status_code}"
    assert r.json()["openai:gpt-4o"]["in"] == 20

    r = client.delete("/api/usage")
    assert r.status_code == 200, f"DELETE /api/usage -> {r.status_code}"
    assert r.json() == {"ok": True}

    assert client.get("/api/usage").json() == {}


def test_the_usage_routes_are_registered_at_the_paths_the_ui_calls():
    """The frontend hardcodes these; a rename anywhere else must fail here."""
    from redirecall import appcore
    reg = {(getattr(r, "path", None), m)
           for r in appcore.app.routes for m in getattr(r, "methods", set())}
    assert ("/api/usage", "GET") in reg
    assert ("/api/usage", "DELETE") in reg


# ── fork endpoint ────────────────────────────────────────────────────────────
def test_fork_copies_prefix_and_leaves_original(monkeypatch):
    sid = f"__rrtest_{os.getpid()}__fork_src"
    msgs = [{"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"}]
    state._sessions[sid] = [dict(m) for m in msgs]
    saved: dict = {}
    monkeypatch.setattr(sessions, "save_session", lambda s, m: saved.update({s: m}))
    try:
        out = routes_misc.api_fork_session(
            sid, {"role": "assistant", "content_prefix": "a1", "occurrence": 1})
        new_sid = out["id"]
        assert out["messages"] == 2
        assert [m["content"] for m in state._sessions[new_sid]] == ["q1", "a1"]
        # deep copy — mutating the fork must not touch the original
        state._sessions[new_sid][0]["content"] = "EDITED"
        assert state._sessions[sid][0]["content"] == "q1"
        assert new_sid in saved
        state._sessions.pop(new_sid, None)
    finally:
        state._sessions.pop(sid, None)


def test_fork_anchor_survives_client_side_extra_turns(monkeypatch):
    """The reason the anchor exists: the client keeps aborted answers the server never
    stored. Anchoring on (role, content, occurrence) must land on the right STORED message
    even though the client's indices are shifted.

    The store deliberately contains a DECOY assistant turn before the target, and the
    target's prefix is shared with a later turn. With a single unique match, dropping the
    prefix test or the occurrence count changes nothing and the test cannot discriminate
    anchoring from plain indexing — which is exactly what it exists to prove.
    """
    sid = f"__rrtest_{os.getpid()}__fork_desync"
    state._sessions[sid] = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "draft answer"},    # decoy: an earlier assistant
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "final answer v1"},  # occurrence 1 of "final"
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "final answer v2"},  # occurrence 2 of "final"
    ]
    monkeypatch.setattr(sessions, "save_session", lambda s, m: None)
    made = []
    try:
        # occurrence 1 must land on the FIRST "final answer", not the decoy and not v2
        out = routes_misc.api_fork_session(
            sid, {"role": "assistant", "content_prefix": "final answer", "occurrence": 1})
        made.append(out["id"])
        got = [m["content"] for m in state._sessions[out["id"]]]
        assert got == ["q1", "draft answer", "q2", "final answer v1"], got

        # occurrence 2 must land on the SECOND — a matcher that ignores the count, or one
        # that takes the last match, gets this wrong
        out2 = routes_misc.api_fork_session(
            sid, {"role": "assistant", "content_prefix": "final answer", "occurrence": 2})
        made.append(out2["id"])
        got2 = [m["content"] for m in state._sessions[out2["id"]]]
        assert got2[-1] == "final answer v2" and len(got2) == 6, got2

        # the decoy is reachable by its own prefix — proving the prefix is really compared
        out3 = routes_misc.api_fork_session(
            sid, {"role": "assistant", "content_prefix": "draft", "occurrence": 1})
        made.append(out3["id"])
        assert [m["content"] for m in state._sessions[out3["id"]]] == ["q1", "draft answer"]
    finally:
        for i in made:
            state._sessions.pop(i, None)
        state._sessions.pop(sid, None)


def test_fork_rejects_bad_anchor():
    from fastapi import HTTPException
    sid = f"__rrtest_{os.getpid()}__fork_bad"
    state._sessions[sid] = [{"role": "user", "content": "x"}]
    try:
        with pytest.raises(HTTPException):   # missing prefix
            routes_misc.api_fork_session(sid, {"role": "user"})
        with pytest.raises(HTTPException):   # bad role
            routes_misc.api_fork_session(sid, {"role": "system", "content_prefix": "x"})
        with pytest.raises(HTTPException):   # not persisted (e.g. aborted turn)
            routes_misc.api_fork_session(
                sid, {"role": "assistant", "content_prefix": "never stored"})
    finally:
        state._sessions.pop(sid, None)


# ── watched folders: candidate scan ──────────────────────────────────────────
def test_watch_candidates_filters_dotdirs_and_extensions(tmp_path):
    from redirecall import ws as ws_mod, ingest
    (tmp_path / "a.md").write_text("hello")
    (tmp_path / "b.txt").write_text("hello")
    (tmp_path / "c.exe").write_text("nope")
    sub = tmp_path / "docs"; sub.mkdir()
    (sub / "d.pdf").write_bytes(b"%PDF-1.4")
    hidden = tmp_path / ".git"; hidden.mkdir()
    (hidden / "e.md").write_text("skip me")
    got = ws_mod._watch_candidates(tmp_path, ingest._CHAT_FILE_ACCEPT)
    assert sorted(p.name for p, _ in got) == ["a.md", "b.txt", "d.pdf"]
    # signatures are "mtime_ns:size" — both stat fields present and non-zero size
    for _, sig in got:
        m, s = sig.split(":")
        assert int(m) > 0 and int(s) > 0


def test_watch_seen_keys_are_instance_scoped(tmp_path, monkeypatch):
    """The same folder feeding two instances must track signatures separately —
    a shared key made the second instance skip every file as already-seen."""
    from redirecall import ws as ws_mod
    import redis as _redis
    from conftest import REDIS_HOST, REDIS_PORT
    from redirecall import redis_store
    key = f"__rrtest_{os.getpid()}__:watchseen"
    monkeypatch.setattr(ws_mod, "_WATCH_SEEN_KEY", key)
    try:
        rc = _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=2)
        rc.ping(); rc.delete(key)
    except Exception:
        pytest.skip("redis not reachable")
    monkeypatch.setattr(redis_store, "r", lambda: rc)
    (tmp_path / "x.md").write_text("hello")
    entries = ws_mod._watch_candidates(tmp_path, {".md"})
    rc.hset(key, f"instA\x00{entries[0][0]}", entries[0][1])
    seen_a, _ = ws_mod._watch_seen_sync("instA", entries, tmp_path)
    seen_b, _ = ws_mod._watch_seen_sync("instB", entries, tmp_path)
    assert seen_a.get(f"instA\x00{entries[0][0]}") == entries[0][1]
    assert f"instB\x00{entries[0][0]}" not in seen_b, \
        "instance B must not inherit instance A's signatures"
    # prune: a recorded file that no longer exists under the root is dropped
    rc.hset(key, f"instA\x00{tmp_path}/gone.md", "1:1")
    _, stale = ws_mod._watch_seen_sync("instA", entries, tmp_path)
    assert stale == [f"instA\x00{tmp_path}/gone.md"]
    assert rc.hget(key, f"instA\x00{tmp_path}/gone.md") is None
    rc.delete(key)
