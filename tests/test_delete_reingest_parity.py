# SPDX-License-Identifier: AGPL-3.0-or-later
"""Delete must release EXACTLY the dedup hashes ingest stored.

ingest_text scopes every dedup hash by source — sha256("{source}\\x00{normalised}")
— but api_delete_document used to release the unscoped sha256(normalised). Nothing
was freed, so a delete followed by re-ingest of the same document dropped every
unchanged chunk as a "duplicate" and the document vanished from the index. The
watched-folder change path (delete-then-re-ingest on every file edit) made this
automatic data loss; the manual Documents-delete + re-upload flow had the same bug.
"""
import hashlib

import pytest


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _ingest_hash(source: str, chunk: str) -> str:
    # mirror of ingest_text's spelling (guarded below against drift)
    return hashlib.sha256(f"{source}\x00{_norm(chunk)}".encode()).hexdigest()


class _FakeRedis:
    """One FT.SEARCH page with our chunk, then empty; records srem args."""

    def __init__(self, source, texts):
        items = []
        for i, t in enumerate(texts):
            items += [f"k{i}".encode(), [b"text", t.encode()]]
        self.pages = [[len(texts)] + items, [0]]
        self.searches = 0
        self.srems = []

    def execute_command(self, *args):
        if args[0] == "FT.SEARCH":
            self.searches += 1
            return self.pages[min(self.searches - 1, len(self.pages) - 1)]
        return None

    def pipeline(self, transaction=False):
        class _P:
            def delete(self, k): pass
            def execute(self): return []
        return _P()

    def srem(self, *a):
        self.srems.append(a)
        return len(a) - 1


def test_delete_releases_the_source_scoped_hashes(app_module, monkeypatch):
    m = app_module
    source = "notes/guide.md"
    texts = ["Alpha beta GAMMA delta.", "Second chunk text here"]
    fake = _FakeRedis(source, texts)
    monkeypatch.setattr(m.rag_admin, "_rc_for", lambda *a, **k: fake)
    monkeypatch.setattr(m.config, "append_log", lambda e: None)

    out = m.api_delete_document(instance="tparity", source=source)
    assert out["ok"] is True, out

    # only the chunk_hashes set matters here (a second srem maintains the
    # sources registry — not part of this contract)
    released = {h for call in fake.srems if "chunk_hashes" in str(call[0])
                for h in call[1:]}
    expected = {_ingest_hash(source, t) for t in texts}
    assert released == expected, (
        f"released hashes do not match ingest's source-scoped spelling —\n"
        f"released: {sorted(released)}\nexpected: {sorted(expected)}\n"
        f"a mismatch means delete+re-ingest silently drops every unchanged chunk")


def test_ingest_hash_spelling_matches_this_test(app_module):
    """Guard the mirror itself: if ingest_text's hash spelling ever changes, this
    file's _ingest_hash must change with it — fail loudly instead of testing a
    stale contract."""
    import inspect
    src = inspect.getsource(app_module.ingest.ingest_text)
    assert 'sha256(f"{source}\\x00{\' \'.join(c.lower().split())}"' in src, (
        "ingest_text's dedup-hash spelling changed — update _ingest_hash here AND "
        "api_delete_document's release spelling together")
