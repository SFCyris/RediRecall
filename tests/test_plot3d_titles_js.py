# SPDX-License-Identifier: AGPL-3.0-or-later
"""B7 — plot3d string-title normalisation was incomplete under Plotly 3.

Plotly 3 ignores the string form of every ``title`` and shows its placeholder
instead. index.html normalises ``string`` → ``{text}`` before ``Plotly.newPlot``,
but this session's patch only covered the plot title, the cartesian axes and the
3-D scene axes. Three title locations were left as bare strings and would drop
silently: ``layout.legend.title``, ``layout.coloraxis.colorbar.title`` and each
trace's own colorbar title (``marker.colorbar.title`` for scatter-like traces,
and the direct ``colorbar.title`` a surface/heatmap carries).

This drives the REAL ``RICH_LANES.plot3d.draw`` under node with a ``Plotly`` stub
that records exactly what layout/data object the lane handed to ``newPlot`` — so
it asserts on the real, post-normalisation spec, not on source text. Each check is
guarded by a mutations.json entry that removes the corresponding normalisation
line and has been shown to make this test go red.
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


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _lane(html: str, name: str) -> str:
    start = html.index("const RICH_LANES={")
    obj = html[start:html.index("\n};\n", start) + 3]
    keys = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ([A-Za-z0-9_]+):\{", obj)]
    for i, (pos, k) in enumerate(keys):
        if k == name:
            return obj[pos:keys[i + 1][0] if i + 1 < len(keys) else len(obj)]
    raise AssertionError(f"{name} is not a key of RICH_LANES")


def _plot(spec: dict) -> dict:
    """Run plot3d.draw(spec) under node against a Plotly stub; return {data, layout}
    exactly as the lane passed them to Plotly.newPlot."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _html()
    js = (
        "let captured=null;\n"
        "const Plotly={ newPlot:(h,data,layout,cfg)=>{ captured={data,layout,cfg};"
        " return Promise.resolve(); }, Plots:{resize(){}} };\n"
        "const window={ Plotly };\n"
        "function El(tag){ return {tag,style:{},children:[],appendChild(c){this.children.push(c);return c}}; }\n"
        "const document={createElement:El,createElementNS:(ns,t)=>El(t)};\n"
        "const RICH_LANES={\n" + _lane(html, "plot3d") + "\n};\n"
        "(async()=>{\n"
        "  const out=El('div');\n"
        f"  await RICH_LANES.plot3d.draw(out, {json.dumps(json.dumps(spec))});\n"
        "  process.stdout.write(JSON.stringify(captured));\n"
        "})().catch(e=>{ console.error(e && e.stack || e); process.exit(3); });\n"
    )
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1600]}"
    return json.loads(r.stdout)


def test_issue_b7_legend_title_string_is_normalised():
    """layout.legend.title:'Series' → {text:'Series'} — was left a bare string and
    dropped under Plotly 3."""
    cap = _plot({"data": [{"type": "scatter3d", "x": [1], "y": [1], "z": [1]}],
                 "layout": {"legend": {"title": "Series"}}})
    assert cap["layout"]["legend"]["title"] == {"text": "Series"}, cap["layout"]["legend"]


def test_issue_b7_coloraxis_colorbar_title_string_is_normalised():
    """layout.coloraxis.colorbar.title:'Energy' → {text:'Energy'}."""
    cap = _plot({"data": [{"type": "surface", "z": [[1, 2], [3, 4]]}],
                 "layout": {"coloraxis": {"colorbar": {"title": "Energy"}}}})
    got = cap["layout"]["coloraxis"]["colorbar"]["title"]
    assert got == {"text": "Energy"}, got


def test_issue_b7_trace_marker_colorbar_title_string_is_normalised():
    """A scatter3d trace's marker.colorbar.title:'Temp' → {text:'Temp'}."""
    cap = _plot({"data": [{"type": "scatter3d", "x": [1], "y": [1], "z": [1],
                           "marker": {"color": [1], "colorbar": {"title": "Temp"}}}],
                 "layout": {}})
    got = cap["data"][0]["marker"]["colorbar"]["title"]
    assert got == {"text": "Temp"}, got


def test_issue_b7_trace_direct_colorbar_title_string_is_normalised():
    """A surface trace's own colorbar.title:'Depth' → {text:'Depth'}."""
    cap = _plot({"data": [{"type": "surface", "z": [[1, 2], [3, 4]],
                           "colorbar": {"title": "Depth"}}], "layout": {}})
    got = cap["data"][0]["colorbar"]["title"]
    assert got == {"text": "Depth"}, got


def test_issue_b7_previously_covered_titles_still_normalise():
    """The plot/axis/scene titles that already worked must keep working — the fix
    extends the set, it does not replace it."""
    cap = _plot({"data": [{"type": "scatter3d", "x": [1], "y": [1], "z": [1]}],
                 "layout": {"title": "Main",
                            "xaxis": {"title": "X"},
                            "scene": {"zaxis": {"title": "Z"}}}})
    lay = cap["layout"]
    assert lay["title"] == {"text": "Main"}, lay["title"]
    assert lay["xaxis"]["title"] == {"text": "X"}, lay["xaxis"]
    assert lay["scene"]["zaxis"]["title"] == {"text": "Z"}, lay["scene"]["zaxis"]


def test_issue_b7_object_titles_are_left_untouched():
    """A title already given as {text:…} (with extra keys like font) must pass through
    unchanged — normalisation only wraps the *string* form."""
    cap = _plot({"data": [{"type": "scatter3d", "x": [1], "y": [1], "z": [1]}],
                 "layout": {"legend": {"title": {"text": "Kept", "font": {"size": 14}}}}})
    assert cap["layout"]["legend"]["title"] == {"text": "Kept", "font": {"size": 14}}
