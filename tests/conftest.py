# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures.

Every test runs against a throwaway DATA_DIR and a dedicated Redis logical DB, so
a test run can never read or overwrite a real install's config, sessions or
vectors. The env var is set before ``redirecall.main`` is imported because the
module resolves DATA_DIR at import time.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_DATA = tempfile.mkdtemp(prefix="redirecall-tests-")
os.environ["REDIRECALL_DATA_DIR"] = _TMP_DATA
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Redis used by the integration tests. RediSearch refuses FT.CREATE on any db but
# 0, so tests MUST share db 0 with real data. They therefore namespace every key
# under a per-run prefix and delete only that prefix — never FLUSHDB, which would
# destroy the user's corpus.
REDIS_HOST = os.environ.get("REDIRECALL_TEST_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIRECALL_TEST_REDIS_PORT", "6390"))
REDIS_DB = 0
KEY_PREFIX = f"__rrtest_{os.getpid()}__"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: reaches the public internet; runs only with REDIRECALL_TEST_NETWORK=1",
    )


@pytest.fixture(scope="session")
def app_module():
    """The imported application module, pointed at the test data dir."""
    import redirecall.main as m
    return m


@pytest.fixture
def cfg(app_module):
    """Restore the global config after any test that mutates it."""
    import copy
    original = copy.deepcopy(app_module._config)
    yield app_module._config
    app_module._config.clear()
    app_module._config.update(original)


@pytest.fixture(scope="session")
def redis_client():
    """A client on the test DB, or skip the test when no server is reachable."""
    import redis
    rc = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    try:
        rc.ping()
    except Exception as e:                                    # pragma: no cover
        pytest.skip(f"no Redis at {REDIS_HOST}:{REDIS_PORT} ({e})")
    return rc


def _purge(rc):
    """Delete only this run's namespaced keys and indexes."""
    for key in rc.scan_iter(f"{KEY_PREFIX}*", count=500):
        rc.delete(key)
    try:
        for name in rc.execute_command("FT._LIST"):
            name = name.decode() if isinstance(name, bytes) else name
            if name.startswith(KEY_PREFIX):
                rc.execute_command("FT.DROPINDEX", name)
    except Exception:
        pass


@pytest.fixture
def clean_redis(redis_client):
    """A client plus a namespace. NEVER flushes — db 0 holds the user's real data.

    Tests must build keys with the ``key(...)`` helper so cleanup can find them.
    """
    _purge(redis_client)
    redis_client.key = lambda suffix: f"{KEY_PREFIX}{suffix}"
    yield redis_client
    _purge(redis_client)


@pytest.fixture
def data_dir():
    """An isolated directory for tests that write config or session files."""
    with tempfile.TemporaryDirectory(prefix="redirecall-case-") as d:
        yield Path(d)
