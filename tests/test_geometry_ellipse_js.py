# SPDX-License-Identifier: AGPL-3.0-or-later
"""The geometry lane's ellipse-arg reshape, run as real JavaScript under node.

Models emit ellipses the SVG way — a centre and x/y radii, ``ellipse([cx,cy],
[rx,ry])`` — but JSXGraph's ``ellipse`` is a conic that needs TWO FOCI plus a
third constraint. Handed the 2-parent SVG form, JSXGraph dereferences an
undefined third parent and throws "Cannot read properties of undefined (reading
'length')", blanking the whole figure (a real report: a camera lens-diagram).

``_geoArgs`` converts centre+radii to the equivalent foci form. These tests run
the actual function extracted from ``index.html`` (not a copy) and check the
geometry of what comes out — the two foci and the point-sum — so the ellipse the
browser draws has exactly the requested radii. Offline: node, no browser.
"""
import json
import math
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _js_fn(html: str, header: str) -> str:
    start = html.index(header)
    return html[start:html.index("\n}", start) + 2]


def _const_stmt(html: str, name: str) -> str:
    start = html.index(f"const {name}=")
    return html[start:html.index(";\n", start) + 1]


def _run(cases_js: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _html()
    # _geoText is only reached by the text branch; stub it so the parse resolves.
    prelude = (
        "const _geoText=v=>String(v).replace(/[<>&]/g,'');\n"
        + _js_fn(html, "function _geoNum(") + "\n"
        + _const_stmt(html, "_GEO_POINT_LIST_TYPES") + "\n"
        + _js_fn(html, "function _geoArgs(") + "\n"
        + "function _sum(px,py,f1,f2){return Math.hypot(px-f1[0],py-f1[1])"
          "+Math.hypot(px-f2[0],py-f2[1]);}\n"
    )
    r = run_node(prelude + cases_js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_tall_svg_ellipse_becomes_foci_form_with_requested_radii():
    """ellipse([5,0],[0.4,3]) — the exact spec that crashed — must come out as a
    3-parent foci conic (never the 2-parent form JSXGraph chokes on), and the
    conic it describes must pass through the requested extremes: the top point
    (5, 3) and the side point (5.4, 0). Both lie on the ellipse iff their
    distance-sum to the two foci equals the major-axis length 2·3 = 6."""
    res = _run(
        "const r=_geoArgs('ellipse',[[5,0],[0.4,3]],{});"
        "console.log(JSON.stringify({len:r.length,third:r[2],f1:r[0],f2:r[1],"
        "top:_sum(5,3,r[0],r[1]),side:_sum(5.4,0,r[0],r[1])}));"
    )
    assert res["len"] == 3, f"not the foci form JSXGraph needs: {res}"
    assert res["f1"][0] == 5 and res["f2"][0] == 5, \
        f"a taller-than-wide ellipse must put its foci on the vertical axis: {res}"
    assert math.isclose(res["third"], 6, abs_tol=1e-9), res
    assert math.isclose(res["top"], 6, abs_tol=1e-6), \
        f"top point (5,3) is not on the ellipse -> ry != 3: {res}"
    assert math.isclose(res["side"], 6, abs_tol=1e-6), \
        f"side point (5.4,0) is not on the ellipse -> rx != 0.4: {res}"


def test_flat_four_number_ellipse_matches_the_nested_form():
    """The flat [cx,cy,rx,ry] spelling must reshape identically to the nested one."""
    res = _run(
        "const r=_geoArgs('ellipse',[5,0,0.4,3],{});"
        "console.log(JSON.stringify({len:r.length,third:r[2],f1:r[0],f2:r[1]}));"
    )
    assert res["len"] == 3 and math.isclose(res["third"], 6, abs_tol=1e-9)
    assert res["f1"] == [5, pytest.approx(-math.sqrt(8.84))]
    assert res["f2"] == [5, pytest.approx(math.sqrt(8.84))]


def test_wide_ellipse_puts_foci_on_the_horizontal_axis():
    """rx > ry: foci on the x-axis at (cx ± √(a²−b²), cy), point-sum 2·rx."""
    res = _run(
        "const r=_geoArgs('ellipse',[[0,0],[5,2]],{});"
        "console.log(JSON.stringify({len:r.length,third:r[2],f1:r[0],f2:r[1]}));"
    )
    c = math.sqrt(25 - 4)
    assert res["len"] == 3 and math.isclose(res["third"], 10, abs_tol=1e-9)
    assert res["f1"] == [pytest.approx(-c), 0] and res["f2"] == [pytest.approx(c), 0]


def test_native_three_parent_foci_ellipse_is_left_untouched():
    """A genuine JSXGraph foci call (two foci + a length) must pass straight
    through — the reshape only rescues the 2-parent / 4-number SVG forms."""
    res = _run(
        "const r=_geoArgs('ellipse',[[-3,0],[3,0],10],{});"
        "console.log(JSON.stringify(r));"
    )
    assert res == [[-3, 0], [3, 0], 10]
