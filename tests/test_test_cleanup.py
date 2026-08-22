# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite's own cleanup, which had no coverage and therefore leaked for months.

Tests share db 0 with the user's real corpus — RediSearch refuses FT.CREATE on any
other db — so they namespace every key under a per-pid prefix and delete only that.
The delete matched the prefix only at the START of a name, and a test that builds a
RAG instance called ``<prefix>kept`` produces ``rag:<prefix>kept:chunk:1`` and an
index ``rag:<prefix>kept:idx``. Both begin with ``rag:``, so neither was matched:
every run left its instances, chunks and indexes behind in the live database.

These assert on real Redis state, because the defect was invisible to every other
kind of check — the suite was fully green while it accumulated 38 stray instances.
"""
import pytest

from conftest import KEY_PREFIX, _purge


@pytest.fixture
def rc(redis_client):
    _purge(redis_client)
    yield redis_client
    _purge(redis_client)


def _names(rc):
    try:
        return [n.decode() if isinstance(n, bytes) else n
                for n in rc.execute_command("FT._LIST")]
    except Exception:                                    # pragma: no cover
        pytest.skip("RediSearch not available")


def test_purge_removes_keys_that_only_contain_the_prefix(rc):
    """The shape the tests actually create: the namespace is in the MIDDLE."""
    rc.set(f"rag_meta:{KEY_PREFIX}kept", "{}")
    rc.set(f"rag:{KEY_PREFIX}kept:chunk:1", "x")
    rc.set(f"{KEY_PREFIX}plain", "x")
    _purge(rc)
    assert rc.exists(f"rag_meta:{KEY_PREFIX}kept") == 0
    assert rc.exists(f"rag:{KEY_PREFIX}kept:chunk:1") == 0
    assert rc.exists(f"{KEY_PREFIX}plain") == 0


def test_purge_drops_indexes_whose_name_only_contains_the_prefix(rc):
    """`rag:<prefix>kept:idx` begins with "rag:", so a startswith test never saw it —
    which is exactly how the leftover indexes accumulated."""
    idx = f"rag:{KEY_PREFIX}dropme:idx"
    rc.execute_command("FT.CREATE", idx, "PREFIX", "1",
                       f"rag:{KEY_PREFIX}dropme:chunk:", "SCHEMA", "text", "TEXT")
    assert idx in _names(rc)
    _purge(rc)
    assert idx not in _names(rc), "the index survived cleanup"


def test_purge_leaves_everything_outside_the_namespace_alone(rc):
    """The prefix carries the pid, but the blast radius still has to be checked: this
    runs against the same database as the user's real corpus."""
    guard = "rag_meta:NotATestInstance"
    pre_existing = rc.exists(guard)
    if not pre_existing:
        rc.set(guard, "keep me")
    try:
        rc.set(f"rag_meta:{KEY_PREFIX}kept", "{}")
        _purge(rc)
        assert rc.exists(guard) == 1, "cleanup deleted a key outside its namespace"
    finally:
        if not pre_existing:
            rc.delete(guard)


def test_a_real_looking_index_is_not_dropped(rc):
    """FT.DROPINDEX on a real instance's index would take out the user's search."""
    idx = f"rag:{KEY_PREFIX}scoped:idx"
    keep = "rag:NotATestInstance:idx"
    existed = keep in _names(rc)
    if not existed:
        rc.execute_command("FT.CREATE", keep, "PREFIX", "1",
                           "rag:NotATestInstance:chunk:", "SCHEMA", "text", "TEXT")
    try:
        rc.execute_command("FT.CREATE", idx, "PREFIX", "1",
                           f"rag:{KEY_PREFIX}scoped:chunk:", "SCHEMA", "text", "TEXT")
        _purge(rc)
        names = _names(rc)
        assert idx not in names
        assert keep in names, "cleanup dropped an index outside its namespace"
    finally:
        if not existed:
            try:
                rc.execute_command("FT.DROPINDEX", keep)
            except Exception:
                pass
