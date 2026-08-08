# SPDX-License-Identifier: AGPL-3.0-or-later
"""B6 — the ```solve lane's simplify/expand were no-ops on their documented cases.

Runs the REAL ``RICH_LANES.solve.draw`` under node against the REAL mathjs 15.1.0
(tests/fixtures/mathjs.umd.js — the same version index.html loads from the CDN),
through a tiny DOM stub, and reads the produced ``<td>`` text back out. Nothing
about the algebra is re-implemented in the test: it asserts on the strings the
lane actually writes into the result table.

Old wrong behaviour (internal/OPEN-ISSUES-1.5.0.md B6):
  * ``simplify: (x^2-1)/(x-1)`` → ``(x ^ 2 - 1) / (x - 1)`` — unchanged. This is the
    exact example documented in main.py:304, so the lane's headline case was broken.
  * ``expand: (x + 1)^2`` → ``(x + 1) ^ 2`` — unchanged, for every input. The lane
    called ``math.simplify(expr,{},{exactFractions:false})``; mathjs read ``{}`` as
    the scope slot, so nothing ever expanded.
  * ``simplify: 2*x + 3*x`` → ``5 * x`` already worked and must keep working.

Each assertion below is paired with an entry in tests/mutations.json that reverts
the lane to the old call and has been shown to make this test go red.
"""
import json
import pathlib
import re
import shutil
import subprocess

from _jsrun import run_node

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "redirecall" / "index.html"
_MATHJS = pathlib.Path(__file__).resolve().parent / "fixtures" / "mathjs.umd.js"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _lane(html: str, name: str) -> str:
    """A single lane's object literal out of RICH_LANES, sliced at the next lane key."""
    start = html.index("const RICH_LANES={")
    obj = html[start:html.index("\n};\n", start) + 3]
    keys = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ([A-Za-z0-9_]+):\{", obj)]
    for i, (pos, k) in enumerate(keys):
        if k == name:
            return obj[pos:keys[i + 1][0] if i + 1 < len(keys) else len(obj)]
    raise AssertionError(f"{name} is not a key of RICH_LANES")


def _algebra_helpers(html: str) -> str:
    """From `function _solveRoots(` up to the RICH_LANES table — captures _solveRoots
    and any simplify/expand helpers added beside it, whether or not the fix is in yet."""
    s = html.index("function _solveRoots(")
    return html[s:html.index("const RICH_LANES={", s)]


def _run(lines):
    """Drive RICH_LANES.solve.draw(out, lines) under node; return the [expr, value]
    rows it wrote, using real mathjs and a minimal DOM stub."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    if not _MATHJS.exists():
        pytest.skip("vendored mathjs fixture missing")
    html = _html()
    js = (
        f"const math = require({json.dumps(str(_MATHJS))});\n"
        "const window = { math };\n"
        "function El(tag){ const e={tag,className:'',_t:'',children:[],style:{},\n"
        "  classList:{add(){},remove(){},contains(){return false}},\n"
        "  appendChild(c){e.children.push(c);return c},\n"
        "  setAttribute(){},getAttribute(){return null}};\n"
        "  Object.defineProperty(e,'textContent',{get(){return e._t},set(v){e._t=String(v)}});\n"
        "  return e; }\n"
        "const document={createElement:El,createElementNS:(ns,t)=>El(t)};\n"
        + _algebra_helpers(html) + "\n"
        "const RICH_LANES={\n" + _lane(html, "solve") + "\n};\n"
        "(async()=>{\n"
        "  const out=El('div');\n"
        f"  await RICH_LANES.solve.draw(out, {json.dumps(lines)});\n"
        "  const table=out.children[0];\n"
        "  const rows=table.children.map(tr=>tr.children.map(td=>td.textContent));\n"
        "  process.stdout.write(JSON.stringify(rows));\n"
        "})().catch(e=>{ console.error(e && e.stack || e); process.exit(3); });\n"
    )
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1600]}"
    return json.loads(r.stdout)


def _val(lines, expr_line):
    """The value cell for a given input line."""
    rows = _run(lines)
    for expr, val in rows:
        if expr.replace(" ", "") == expr_line.replace(" ", ""):
            return val
    raise AssertionError(f"no row for {expr_line!r} in {rows}")


# ── B6 simplify ────────────────────────────────────────────────────────────────
def test_issue_b6_simplify_cancels_the_documented_example():
    """simplify:(x^2-1)/(x-1) must reduce to x + 1 — it was returned unchanged, and
    it is the exact example printed in the base instruction (main.py:304)."""
    v = _val("simplify: (x^2-1)/(x-1)", "simplify: (x^2-1)/(x-1)")
    assert v == "x + 1", v
    assert "/" not in v and "^" not in v, f"still an unsimplified rational: {v!r}"


def test_issue_b6_simplify_other_rationals_reduce():
    """A few more exact-division rationals, to prove it is real polynomial division
    and not a hard-coded string for the one documented case."""
    assert _val("simplify: (x^2-4)/(x-2)", "simplify: (x^2-4)/(x-2)") == "x + 2"
    assert _val("simplify: (x^3-1)/(x-1)", "simplify: (x^3-1)/(x-1)") == "x ^ 2 + x + 1"


def test_issue_b6_simplify_non_dividing_rational_is_left_alone():
    """(x^2+1)/(x-1) does not divide evenly; it must NOT be mangled — the lane falls
    back to plain simplify, which leaves it as the rational it is."""
    v = _val("simplify: (x^2+1)/(x-1)", "simplify: (x^2+1)/(x-1)")
    assert "/" in v, f"a non-dividing rational was wrongly rewritten: {v!r}"


def test_issue_b6_simplify_like_terms_still_work():
    """The one case that already worked (2*x + 3*x → 5 * x) must not regress."""
    assert _val("simplify: 2*x + 3*x", "simplify: 2*x + 3*x") == "5 * x"


# ── B6 expand ──────────────────────────────────────────────────────────────────
def test_issue_b6_expand_binomial_square():
    """expand:(x + 1)^2 must become x^2 + 2x + 1 — it was returned unchanged for
    every input because of the {}-as-scope bug."""
    v = _val("expand: (x + 1)^2", "expand: (x + 1)^2")
    assert v == "x ^ 2 + 2 * x + 1", v


def test_issue_b6_expand_product_and_cube():
    assert _val("expand: (x+2)*(x-3)", "expand: (x+2)*(x-3)") == "x ^ 2 - x - 6"
    assert _val("expand: (x+1)^3", "expand: (x+1)^3") == "x ^ 3 + 3 * x ^ 2 + 3 * x + 1"


def test_issue_b6_derivative_and_roots_unaffected():
    """The sibling operations share the lane and must be untouched by the fix."""
    assert _val("derivative: x^3 + 2x", "derivative: x^3 + 2x") == "3 * x ^ 2 + 2"
    assert _val("solve: x^2 - 5x + 6 = 0", "solve: x^2 - 5x + 6 = 0") == "x = 3 , 2"
