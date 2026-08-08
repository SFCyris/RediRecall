# Regression tests

Run them:

```bash
venv/bin/python3 -m pytest tests/ -q
```

Tests that need Redis look for one on `127.0.0.1:6390` and **skip** if none is
reachable, so the suite still runs on a bare checkout. Override with
`REDIRECALL_TEST_REDIS_HOST` / `REDIRECALL_TEST_REDIS_PORT`.

Two tests reach outside the process:

* frontend tests under `test_lanes_js.py` shell out to `node` and **skip** without
  it. They need no browser, no network and no dev server.
* `test_manifest_urls_are_live` HEADs every CDN asset and only runs with
  `REDIRECALL_TEST_NETWORK=1`, because the mutation sweep runs the suite ~76 times
  and would otherwise issue ~1,800 requests to cdnjs per sweep.

## Safety

RediSearch refuses `FT.CREATE` on any database but 0, so these tests share db 0
with real data. They therefore namespace every key under `__rrtest_<pid>__` and
delete only that prefix. **Never add a `flushdb()` to a fixture** — it would
destroy the corpus of whoever runs the suite. Use the `clean_redis` fixture and
build keys with `rc.key("...")`.

Tests that touch config or sessions get an isolated `DATA_DIR`; `conftest.py`
sets `REDIRECALL_DATA_DIR` before `redirecall.main` is imported, because the
module resolves that path at import time.

## The gate: `tests/mutation_sweep.py`

```bash
venv/bin/python3 tests/mutation_sweep.py -j 4          # whole catalogue, ~8 min
venv/bin/python3 tests/mutation_sweep.py -k M16        # one entry, ~25 s
venv/bin/python3 tests/mutation_sweep.py --check       # catalogue freshness, ~1 s
```

`tests/mutations.json` is a catalogue of known-bad edits — each one reintroduces a
defect that has actually shipped. The sweep copies the repo to a throwaway lab
under `$TMPDIR` (venv symlinked, `.git`/caches skipped, **the working tree is
never written**), applies exactly one edit, runs the suite, and reports:

| verdict | meaning |
|---|---|
| `KILLED` | at least one test failed — that defect is guarded |
| `SURVIVED` | the suite stayed green — **the defect can ship again** |
| `STALE` | the `find` text is gone — the catalogue lies; fix it |

Exit code is 1 if anything survived or is stale, so CI can gate on it. A pytest
timeout counts as a kill (a mutation that hangs the suite was noticed). On a full
run it also lists every test that never went red anywhere in the sweep — a test in
that list has not been shown capable of failing.

`--check` is fast enough for a pre-commit hook, which is what keeps a `find`
string from drifting out of the tree and turning a row silently `STALE`.

## Adding a test when you fix a bug

This file exists so a fixed defect stays fixed. For every non-trivial fix:

1. Write the test **first** and watch it fail against the unfixed code. A test
   that has never failed proves nothing.
2. Name it `test_issue<N>_<what_must_hold>` when it maps to a numbered issue in
   `internal/OPEN-ISSUES.md`, otherwise `test_<what_must_hold>`.
3. State the **old wrong behaviour** in the docstring, not the new correct one.
   Six months from now the useful information is what went wrong.
4. **Assert the behaviour.** Call the function, drive the handler, execute the
   JavaScript. A source-level assertion (`inspect.getsource`, reading
   `index.html`) is admissible only when it is accompanied by an entry in
   `tests/mutations.json` that has been **shown to KILL it**, and the sweep must
   be green before merge. There is no "where you can't" exemption — that clause
   used to live here, and 46 of 63 tests took it.

## How source-level assertions stop working

This is not hypothetical. A full sweep against the 1.5.0 suite scored 66 %:
24 of 71 mutations survived, and 17 of 63 tests never failed under any of them.
Four mechanisms accounted for all of it.

**Comment-carrier.** The assertion is satisfied by the prose that explains the
fix. `"casesensitive" in getsource(_get_rag_index).lower()` — the attribute is
spelled `case_sensitive`, so the only carrier was the comment above it; deleting
the attribute kept the suite green. Same for the gantt lane's `useWidth`.
*Countermeasure:* every source assertion goes through `_code()`, which strips
whole-line `#` and `//` comments, and matches the code form (`"case_sensitive": True`,
`tickInterval:'`) rather than a bare word.

**Existence, not enforcement.** `hasattr(m, "_MAX_UPLOAD_BYTES")` and the name
appearing in the handler both survive `if False:  # _MAX_UPLOAD_BYTES`. Nine tests
asserted a constant exists and nothing asserted it is applied.
*Countermeasure:* post the oversized upload, submit the oversized feedback field,
race eight threads at the lock, record what the embedder actually received.

**Fixed-window slicing.** `switchSession()` is 6,934 characters; the test sliced
400. An early `return` added past the window cleared neither `_pendingRegen` nor
the source scope. `html[html.index("const RICH_LANES={"):]` sliced 92,322
characters — to end of file — so a lane key pasted anywhere later satisfied it.
*Countermeasure:* `_js_fn()` slices to the next `function` marker; `_rich_lanes_src()`
stops at the object's own closing brace; the lane registry is parsed by `node`.

**Assertions that cannot discriminate.** `test_issue5_overlap_is_preserved`
measured overlap by word-set intersection over a corpus whose every sentence read
"sentence*i* about databases and storage systems" — the filler alone made every
boundary intersect (measured 1.000 against a 0.50 threshold **with the overlap
carry-back deleted**). `test_issue13` sliced from `append_log(` to the end of the
function and asserted `"except" in tail`, which the handler's outer `except` also
satisfies. `test_issue8` asserted that two sha256 digests computed inside the test
differ.
*Countermeasure:* measure shared sentence identity (0.505 vs 0.000), make
`append_log` raise and check the response, assert the production hash expression.

## Coverage and its limits

The suite covers backend logic, Redis integration, executed frontend JavaScript
and source-level invariants that a catalogued mutation has been shown to break.
It still does **not** drive a real browser, so layout, paint and CSS remain
verified by hand — a lane that parses, runs and sanitises here can still lay out
wrong. `KILLED` is also not the same as "behavioural": a few kills come from
renaming an identifier a test greps for, which would not catch a semantic break
that keeps the name.

The catalogue is not exhaustive either — it targets roughly one defect per
guarded behaviour, and whole surfaces (the math-sentinel pipeline, chart zoom
ranges, molecule card clipping, geometry dark mode) have no entry at all. Every
new fix must arrive with its own entry, or the score will look healthy while the
same class of defect ships again.
