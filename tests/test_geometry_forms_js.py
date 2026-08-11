# SPDX-License-Identifier: AGPL-3.0-or-later
"""SVG-convention arg/attr translations for the geometry lane, run as real JS.

Models describe shapes the SVG / common-math way — a circle as centre+radius, an arc
as centre+radius+angles, a dashed line as stroke-dasharray — but JSXGraph wants its
own parent shapes and a dash *style index*. Fed the natural form, JSXGraph either
throws (blanking the figure) or silently mis-renders. ``_geoArgs`` / ``_geoAttrs``
translate the common forms; the relational types that need a reference to another
element (perpendicular, parallel, tangent, intersection, glider) cannot be expressed
in this coordinate-only schema and are not advertised.

These tests extract the REAL helpers from ``index.html`` and run them under node, so
they check the actual translation output, not a copy. Offline: node, no browser. The
end-to-end render of every form (and the skip-and-footnote resilience path) is covered
by the browser probe in ``visual_probe.py`` / ``test_visual_lanes_browser.py``.
"""
import json
import math
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _prelude() -> str:
    """The whole geometry-helper block verbatim: the _GEO_* consts through _geoBox."""
    html = _INDEX.read_text(encoding="utf-8")
    start = html.index("const _GEO_TYPES=new Set([")
    end = html.index("\n}", html.index("function _geoBox(")) + 2
    block = html[start:end]
    assert "_GEO_TYPE_ALIAS" in block and "function _geoArgs(" in block \
        and "function _geoBox(" in block
    return block


def _val(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _prelude() + f"\nconsole.log(JSON.stringify({expr}));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── arg reshapes ─────────────────────────────────────────────────────────────
def test_point_single_nested_pair_is_unwrapped():
    assert _val("_geoArgs('point',[[3,2]],{})") == [3, 2]


def test_circle_flat_centre_radius_becomes_point_number():
    assert _val("_geoArgs('circle',[0,0,2],{})") == [[0, 0], 2]


def test_circle_radius_from_attrs_is_pulled_into_args():
    assert _val("_geoArgs('circle',[[0,0]],{radius:2})") == [[0, 0], 2]


def test_circle_native_two_point_and_three_point_forms_pass_through():
    assert _val("_geoArgs('circle',[[0,0],[2,0]],{})") == [[0, 0], [2, 0]]
    assert _val("_geoArgs('circle',[[0,0],[2,0],[0,2]],{})") == [[0, 0], [2, 0], [0, 2]]


@pytest.mark.parametrize("kind", ["arc", "sector"])
def test_arc_sector_centre_radius_angles_become_three_points(kind):
    """centre+radius+angles(deg) → [centre, start-on-circle, end-on-circle]. Both the
    nested [[cx,cy],r,a0,a1] and flat [cx,cy,r,a0,a1] spellings; angles in DEGREES."""
    for expr in (f"_geoArgs('{kind}',[[0,0],2,0,90],{{}})",
                 f"_geoArgs('{kind}',[0,0,2,0,90],{{}})"):
        r = _val(expr)
        assert r[0] == [0, 0], f"{expr}: centre wrong -> {r}"
        assert r[1][0] == pytest.approx(2) and r[1][1] == pytest.approx(0, abs=1e-9), r
        assert r[2][0] == pytest.approx(0, abs=1e-9) and r[2][1] == pytest.approx(2), r


def test_arc_native_three_point_forms_pass_through():
    assert _val("_geoArgs('arc',[[0,0],[2,0],[0,2]],{})") == [[0, 0], [2, 0], [0, 2]]
    assert _val("_geoArgs('arc',[0,0,2,0,0,2],{})") == [[0, 0], [2, 0], [0, 2]]


def test_midpoint_flat_reshapes_to_two_points():
    assert _val("_geoArgs('midpoint',[0,0,4,2],{})") == [[0, 0], [4, 2]]


def test_bisector_flat_reshapes_to_three_points():
    assert _val("_geoArgs('bisector',[1,0,0,0,0,1],{})") == [[1, 0], [0, 0], [0, 1]]


# ── dash attr coercion (T6) ──────────────────────────────────────────────────
@pytest.mark.parametrize("dash_in,expected", [
    ("[5,5]", 2),      # SVG stroke-dasharray -> a dashed style (was: rendered SOLID)
    ("'5,5'", 2),      # dash as a string     -> (was: THREW in the renderer)
    ("9", 6),          # out-of-range index   -> clamped (was: THREW)
    ("2", 2),          # already a valid index -> unchanged
    ("true", 2),       # boolean dashed        -> a style
    ("[]", 0),         # empty array           -> no dash
    ("0", 0),          # explicit no-dash
])
def test_dash_is_coerced_to_a_valid_style_index(dash_in, expected):
    assert _val(f"_geoAttrs('line',{{dash:{dash_in}}}).dash") == expected


# ── rect / rectangle (SVG <rect> has no JSXGraph equivalent — it's a polygon) ──
def test_rect_two_opposite_corners_expand_to_four_polygon_corners():
    assert _val("_geoArgs('rect',[[9,-0.5],[10,0.5]],{})") == \
        [[9, -0.5], [10, -0.5], [10, 0.5], [9, 0.5]]


def test_rect_flat_four_numbers_expand_to_four_corners():
    assert _val("_geoArgs('rect',[0,0,2,1],{})") == [[0, 0], [2, 0], [2, 1], [0, 1]]


def test_rect_and_rectangle_are_supported_and_aliased_to_polygon():
    assert _val("[_GEO_TYPES.has('rect'), _GEO_TYPES.has('rectangle'),"
                " _GEO_TYPE_ALIAS['rect'], _GEO_TYPE_ALIAS['rectangle']]") == \
        [True, True, "polygon", "polygon"]


# ── control-point hiding: filled shapes / arcs are not drag constructions ─────
def test_polygon_and_rect_hide_their_vertices():
    assert _val("_geoAttrs('polygon',{}).vertices") == {"visible": False, "fixed": True}
    assert _val("_geoAttrs('rect',{}).vertices") == {"visible": False, "fixed": True}


def test_arc_and_sector_hide_their_control_points():
    assert _val("_geoAttrs('arc',{}).center") == {"visible": False}
    assert _val("_geoAttrs('sector',{}).anglepoint") == {"visible": False}


def test_explicit_point_and_midpoint_keep_their_dots():
    # the hiding must NOT leak onto elements the model wants visible
    assert _val("_geoAttrs('point',{}).vertices === undefined") is True
    assert _val("_geoAttrs('midpoint',{}).center === undefined") is True


# ── lenient bounding box (a bad box must not blank a good figure) ─────────────
def test_geo_box_coerces_quoted_numbers_and_defaults_on_garbage():
    assert _val("_geoBox(['-6','6','6','-6'])") == [-6, 6, 6, -6]   # a model quirk
    assert _val("_geoBox([-1,3,11,-3])") == [-1, 3, 11, -3]          # clean box kept
    assert _val("_geoBox([1,2,3])") == [-6, 6, 6, -6]                # too short -> default
    assert _val("_geoBox([0,'x',1,2])") == [-6, 6, 6, -6]            # NaN -> default
    assert _val("_geoBox('nope')") == [-6, 6, 6, -6]                 # not an array -> default
    assert _val("_geoBox([5,5,5,5])") == [-6, 6, 6, -6]              # zero-area -> default
    assert _val("_geoBox([-6,5,6,5])") == [-6, 6, 6, -6]             # zero height -> default


# ── type set / alias ─────────────────────────────────────────────────────────
def test_vector_is_supported_and_aliased_to_arrow():
    assert _val("[_GEO_TYPES.has('vector'), _GEO_TYPE_ALIAS['vector']]") == [True, "arrow"]


@pytest.mark.parametrize("t", ["perpendicular", "parallel", "tangent", "intersection", "glider"])
def test_reference_only_relational_types_are_not_advertised(t):
    """These need a reference to another element, impossible in a coordinate-only
    element; advertising them only produced figure-blanking throws."""
    assert _val(f"_GEO_TYPES.has('{t}')") is False
