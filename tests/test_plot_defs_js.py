# SPDX-License-Identifier: AGPL-3.0-or-later
"""The function plot lists every function's DEFINITION in front of the graph.

A legend that says only "f(x)", "g(x)", "h(x)" does not tell the reader what those
functions are. `_plotDefsHtml(spec)` builds a coloured block — one "name(x) = expr"
line per function — that `renderPlotBlocks` inserts immediately before the plot
card. This drives the REAL `_plotDefsHtml` under node (with the same `_plotEsc` and
`_plotNum` helpers it depends on) and asserts on the HTML it returns, so it fails if
the block stops listing the expressions, mis-colours them, or swallows the domain
line as if it were a function. Guarded by mutations.json entries shown to make it red.
"""
import pathlib
import re
import shutil

from _jsrun import run_node

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "redirecall" / "index.html"


def _fn(html: str, header: str) -> str:
    """Return a top-level `function name(...) { ... }` body by brace matching."""
    i = html.index(header)
    b = html.index("{", i)
    depth, j = 0, b
    while j < len(html):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[i:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces for {header!r}")


def _defs(spec: str) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    # _PLOT_DEF is a const, not a function: pull its literal out of the source rather
    # than re-typing it here, or the test would validate a copy of the regex instead of
    # the one the app runs.
    m = re.search(r"^const _PLOT_DEF=(/.+/);$", html, re.M)
    assert m, "the shared definition-line regex is gone"
    js = (
        "const math = { evaluate:(s)=>Number(s) };\n"
        + f"const _PLOT_DEF={m.group(1)};\n"
        + _fn(html, "function _plotDefLabel(") + "\n"
        + _fn(html, "function _plotEsc(") + "\n"
        + _fn(html, "function _plotNum(") + "\n"
        + _fn(html, "function _plotDefsHtml(") + "\n"
        + "process.stdout.write(_plotDefsHtml(" + _json(spec) + "));\n"
    )
    r = run_node(js, timeout=30)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:800]}"
    return r.stdout


def _json(s: str) -> str:
    import json
    return json.dumps(s)


def test_defs_list_every_function_expression():
    """The block must contain each function's actual expression, not just f(x)."""
    html = _defs("f(x) = sin(x)\ng(x) = cos(x)\nh(x) = tan(x)\nx = -3.14 .. 3.14")
    for expr in ("sin(x)", "cos(x)", "tan(x)"):
        assert expr in html, f"{expr} missing from the definitions block"
    for name in ("f(x)", "g(x)", "h(x)"):
        assert name in html
    assert "plot-defs-block" in html


def test_defs_colour_matches_the_curve_order():
    """Each definition carries the colour of its curve, in the same order the lane
    assigns them (blue, red, green, …)."""
    html = _defs("f(x) = sin(x)\ng(x) = cos(x)\nh(x) = tan(x)")
    assert html.index("#2563eb") < html.index("#dc2626") < html.index("#059669")
    assert "sin(x)" in html and html.index("#2563eb") < html.index("sin(x)")


def test_defs_do_not_treat_the_domain_line_as_a_function():
    """`x = -3.14 .. 3.14` is the domain, not a function; it must not appear as a
    definition (the earlier bug rendered it as `x(x) = -3.14 .. 3.14`)."""
    html = _defs("f(x) = sin(x)\nx = -3.14 .. 3.14")
    assert "sin(x)" in html
    assert "x(x)" not in html
    assert "-3.14" not in html
    assert html.count("class=\"plot-def\"") == 1


def test_defs_handle_an_unnamed_single_function():
    """A bare expression with no name is listed as `y = expr`."""
    html = _defs("x^2 + 3x - 2\nx = -6 .. 4")
    assert "x^2 + 3x - 2" in html
    assert "<b>y</b>" in html


def test_defs_are_inserted_in_front_of_the_card():
    """renderPlotBlocks must place the block BEFORE the plot card, in the message
    flow — not inside the plot output where it would re-render on every slider tick."""
    html = _INDEX.read_text(encoding="utf-8")
    fn = _fn(html, "function renderPlotBlocks(")
    assert "_plotDefsHtml(spec)" in fn, "renderPlotBlocks does not build the defs block"
    assert "insertAdjacentHTML('beforebegin'" in fn, "defs are not inserted before the card"
    # and the plot builder itself must NOT re-embed them (they'd duplicate on re-render)
    builder = _fn(html, "function _buildFunctionPlot(")
    assert "plot-defs" not in builder, "defs leaked back into the re-rendered SVG"


def test_a_definition_whose_argument_is_not_x_is_still_a_definition():
    """A log-log relation is written log(N) = -D * log(x). Only a literal "(x)" used to
    be accepted as a definition, so the whole line fell through as the expression — and
    mathjs read it as *defining* a function called log, which evaluates to a function
    rather than a number. Every sample became NaN and the plot died with a generic
    "no finite values"."""
    html = _defs("log(N_line) = -1 * log(x) + 0\n"
                 "log(N_Koch) = -1.262 * log(x) + 0\n"
                 "x = 0.01 .. 1")
    assert "log(N_line)" in html and "log(N_Koch)" in html, html
    # the RHS is the expression, and the LHS is not repeated into it
    assert "-1 * log(x) + 0" in html
    assert "log(N_line) = -1" not in html.replace("</b> = ", "</b>|")


def test_the_argument_name_is_preserved_verbatim():
    html = _defs("f(t) = t*2\nx = 0 .. 1")
    assert "f(t)" in html, html
    assert "f(x)" not in html


def test_a_bare_name_still_gains_the_x_argument():
    """`y = sin(x)` has always been labelled y(x); the broader match must not change it."""
    html = _defs("y = sin(x)\nx = 0 .. 1")
    assert "y(x)" in html, html
