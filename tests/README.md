# Regression tests

Run them:

```bash
venv/bin/python3 -m pytest tests/ -q
```

Tests that need Redis look for one on `127.0.0.1:6390` and **skip** if none is
reachable, so the suite still runs on a bare checkout. Override with
`REDIRECALL_TEST_REDIS_HOST` / `REDIRECALL_TEST_REDIS_PORT`.

## Safety

RediSearch refuses `FT.CREATE` on any database but 0, so these tests share db 0
with real data. They therefore namespace every key under `__rrtest_<pid>__` and
delete only that prefix. **Never add a `flushdb()` to a fixture** — it would
destroy the corpus of whoever runs the suite. Use the `clean_redis` fixture and
build keys with `rc.key("...")`.

Tests that touch config or sessions get an isolated `DATA_DIR`; `conftest.py`
sets `REDIRECALL_DATA_DIR` before `redirecall.main` is imported, because the
module resolves that path at import time.

## Adding a test when you fix a bug

This file exists so a fixed defect stays fixed. For every non-trivial fix:

1. Write the test **first** and watch it fail against the unfixed code. A test
   that has never failed proves nothing — several entries here were written
   against defects that turned out not to reproduce, and that is exactly what
   the failing-first step is for.
2. Name it `test_issue<N>_<what_must_hold>` when it maps to a numbered issue in
   `internal/OPEN-ISSUES.md`, otherwise `test_<what_must_hold>`.
3. State the **old wrong behaviour** in the docstring, not the new correct one.
   Six months from now the useful information is what went wrong.
4. Assert the behaviour, not the implementation, where you can. Where you can't
   — frontend code with no test harness, or a constant that has to exist — a
   source-level assertion is acceptable; several here are, and they are marked
   as such by asserting on `inspect.getsource` or the HTML.

## Coverage and its limits

The suite covers backend logic, Redis integration and source-level frontend
invariants. It does **not** drive a browser, so DOM behaviour (rendering,
sorting, badge gating) is asserted against the source that produces it and
verified separately by hand. A change that keeps the source pattern but breaks
the runtime behaviour would pass here.
