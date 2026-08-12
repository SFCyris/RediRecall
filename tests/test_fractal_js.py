# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ```fractal lane's pure helpers, extracted from index.html and run under
node: spec normalisation with hard caps (a hallucinated spec must not be able to
spin the tab), preset resolution, L-system expansion with its size guard, and the
charset rules that keep the lane data-only. Offline — no browser, no canvas
(the renderers themselves are pixel-verified in a real browser)."""
import json
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _helpers() -> str:
    html = _INDEX.read_text(encoding="utf-8")
    start = html.index("const _FRAC_MAX_ITER")
    end = html.index("function _fracDraw(")   # renderers need a canvas; stop before
    return html[start:end]


def _val(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _helpers() + f"\nconsole.log(JSON.stringify((()=>{{try{{return {expr};}}catch(e){{return {{__err:String(e.message||e)}};}}}})()));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── presets ──────────────────────────────────────────────────────────────────
def test_fern_preset_resolves_to_barnsleys_four_maps():
    s = _val("_fracSpec('{\"type\":\"fern\"}')")
    assert s["type"] == "ifs" and len(s["maps"]) == 4
    assert s["maps"][1][:6] == [0.85, 0.04, -0.04, 0.85, 0, 1.6]   # the stem-to-tip map


def test_julia_preset_supplies_c_and_user_c_overrides_it():
    s = _val("_fracSpec('{\"type\":\"julia\"}')")
    assert s["c"] == [-0.8, 0.156]
    s2 = _val("_fracSpec('{\"type\":\"julia\",\"c\":[0.285,0.01]}')")
    assert s2["c"] == [0.285, 0.01]


def test_lsystem_presets_resolve():
    for name in ("dragon", "koch", "plant"):
        s = _val(f"_fracSpec('{{\"type\":\"{name}\"}}')")
        assert s["type"] == "lsystem" and s["rules"], name


# ── caps: a hallucinated spec must be bounded ────────────────────────────────
def test_iter_points_and_depth_are_clamped():
    s = _val("_fracSpec('{\"type\":\"mandelbrot\",\"iter\":999999,\"zoom\":1e99}')")
    assert s["iter"] == 1000 and s["zoom"] == 1e12
    s2 = _val("_fracSpec('{\"type\":\"ifs\",\"maps\":[[0.5,0,0,0.5,0,0]],\"points\":99999999}')")
    assert s2["points"] == 200000
    s3 = _val("_fracSpec('{\"type\":\"lsystem\",\"axiom\":\"F\",\"rules\":{\"F\":\"FF\"},\"depth\":99}')")
    assert s3["depth"] == 14


def test_lsystem_expansion_size_guard_throws():
    # F -> FFFF quadruples per level: depth 14 blows through the cap and must abort
    r = _val("_fracLExpand('F',{F:'FFFF'},14,120000)")
    assert isinstance(r, dict) and "exceeds" in r["__err"]


def test_lsystem_expansion_is_correct():
    assert _val("_fracLExpand('F',{F:'F+F'},2,120000)") == "F+F+F+F"
    # letters without a rule pass through unchanged (X/Y structure carriers)
    assert _val("_fracLExpand('X',{X:'FX'},3,120000)") == "FFFX"


# ── data-only charset rules ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    '{"type":"lsystem","axiom":"F;alert(1)","rules":{}}',
    '{"type":"lsystem","axiom":"F","rules":{"F":"F<script>"}}',
    '{"type":"lsystem","axiom":"F","rules":{"FX":"F"}}',
])
def test_lsystem_rejects_non_turtle_strings(bad):
    r = _val(f"_fracSpec('{bad}')")
    assert isinstance(r, dict) and "__err" in r, f"accepted: {bad}"


def test_ifs_rejects_non_numeric_maps():
    r = _val("_fracSpec('{\"type\":\"ifs\",\"maps\":[[\"a\",0,0,0.5,0,0]]}')")
    assert isinstance(r, dict) and "numbers only" in r["__err"]


def test_unknown_type_and_garbage_json_error_clearly():
    r = _val("_fracSpec('{\"type\":\"donut\"}')")
    assert "unknown fractal type" in r["__err"]
    r2 = _val("_fracSpec('not json')")
    assert "must be JSON" in r2["__err"]


def test_palette_is_whitelisted():
    s = _val("_fracSpec('{\"type\":\"mandelbrot\",\"palette\":\"<img onerror=x>\"}')")
    assert s["palette"] == "viridis"


# ── palette maths ────────────────────────────────────────────────────────────
def test_palette_interpolates_within_rgb_bounds():
    v = _val("[0,0.25,0.5,0.75,1].map(t=>_fracPalette('fire',false).at(t))")
    for c in v:
        assert len(c) == 3 and all(0 <= x <= 255 for x in c)
    assert v[0] != v[-1], "palette endpoints must differ"


# ── prompt steering: the lane existing is not enough — the model must be told
# NOT to fake a fractal with ```geometry or ```svg (a real report: asked for an
# IFS + Julia set example, the model hand-drew a 5-polygon "Sierpinski" in
# ```geometry and a static blob in ```svg captioned "Julia Set Approximation" —
# ```fractal was never used even though it was available and documented). ──────
def test_prompt_directs_fractals_and_ifs_and_julia_to_the_fractal_lane():
    from redirecall import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    i = t.index("```fractal —")
    bullet = t[i:t.index("\n", i)]
    assert "REQUIRED" in bullet
    for kw in ("Mandelbrot", "Julia", "IFS", "L-system"):
        assert kw in bullet, f"{kw!r} missing from the fractal bullet"
    assert "```geometry" in bullet and "```svg" in bullet, \
        "the fractal bullet must name the two lanes the model reached for instead"


def test_geometry_and_svg_bullets_point_back_to_fractal():
    from redirecall import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    geo = t[t.index("```geometry —"):t.index("\n", t.index("```geometry —"))]
    assert "```fractal" in geo, "the geometry bullet must disclaim fractals in favour of ```fractal"
    svg = t[t.index("```abc —"):t.index("\n", t.index("```abc —"))]
    assert "```fractal" in svg, "the svg fallback bullet must disclaim fractals too"


def test_prompt_tells_the_model_to_use_decimals_not_fractions():
    from redirecall import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    bullet = t[t.index("```fractal —"):t.index("\n", t.index("```fractal —"))]
    assert "1/3" in bullet and "0.333" in bullet, \
        "the fractal bullet must show the fraction-vs-decimal example that provoked this"


# ── fraction-literal reshape: a real report ──────────────────────────────────
# The model wrote IFS probabilities as fractions — [...,1/3] — valid maths,
# invalid JSON (`JSON.parse` threw "Expected ',' or ']' after array element"
# and blanked the card). Rewrite NUMBER/NUMBER to its decimal value first,
# via parseFloat arithmetic only — never eval — so this can't become a code path.
def test_bare_fraction_becomes_its_decimal_value():
    assert _val("_fracFixFractions('[1/3]')") == "[0.3333333333333333]"
    assert _val("_fracFixFractions('[1/2, 3/4]')") == "[0.5, 0.75]"


def test_fixed_source_actually_parses_and_the_real_reported_spec_now_renders():
    spec = ('{"type":"ifs","maps":[[0.5,0,0,0.5,0,0,1/3],'
            '[0.5,0,0,0.5,0.5,0,1/3],[0.5,0,0,0.5,0.25,0.433,1/3]],"iter":50000}')
    out = _val(f"_fracSpec({json.dumps(spec)})")
    assert not isinstance(out, dict) or "__err" not in out, out
    assert out["type"] == "ifs" and len(out["maps"]) == 3
    for m in out["maps"]:
        assert abs(m[6] - 1 / 3) < 1e-9, m


def test_quoted_strings_are_never_touched_by_the_fraction_rewrite():
    # no field in this schema is a "N/N"-shaped string, but the protection must
    # hold regardless — a slash inside quotes is never arithmetic
    assert _val("_fracFixFractions('{\"palette\":\"1/3\"}')") == '{"palette":"1/3"}'


def test_division_by_a_literal_zero_is_left_alone():
    # 1/0 stays untouched so JSON.parse still reports a clear, honest error
    # instead of silently emitting Infinity into a numeric field
    assert _val("_fracFixFractions('[1/0]')") == "[1/0]"


def test_ordinary_numbers_and_negative_numbers_are_unaffected():
    assert _val("_fracFixFractions('[0.5, -0.25, 12]')") == "[0.5, -0.25, 12]"
