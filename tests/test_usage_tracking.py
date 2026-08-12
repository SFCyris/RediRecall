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
    return asyncio.new_event_loop().run_until_complete(coro)


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
    """The reason the anchor exists: the client keeps aborted answers the server
    never stored. Anchoring on (role, content, occurrence) must land on the right
    stored message even though the client's indices are shifted."""
    sid = f"__rrtest_{os.getpid()}__fork_desync"
    # server-side store — NO aborted partial in here
    state._sessions[sid] = [{"role": "user", "content": "q2"},
                            {"role": "assistant", "content": "a2"},
                            {"role": "user", "content": "q3"},
                            {"role": "assistant", "content": "a3"}]
    monkeypatch.setattr(sessions, "save_session", lambda s, m: None)
    try:
        # the client wanted to fork at a2 — with the aborted pair before it, its
        # local index would have been 3 (which is a3 server-side, the old bug)
        out = routes_misc.api_fork_session(
            sid, {"role": "assistant", "content_prefix": "a2", "occurrence": 1})
        forked = state._sessions[out["id"]]
        assert [m["content"] for m in forked] == ["q2", "a2"], \
            f"anchor landed on the wrong message: {[m['content'] for m in forked]}"
        state._sessions.pop(out["id"], None)
    finally:
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
