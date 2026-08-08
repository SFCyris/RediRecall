# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rich-render lane tests that EXECUTE the frontend code instead of grepping it.

A lane's ``draw(out, src, uid)`` needs no browser: it is handed an element-like
object and a source string. Extracting the lane out of ``index.html`` and running
it under node with stub globals catches three things a string match cannot:

  * **an API break.** viz-js 3.x removed the ``new Viz()`` constructor. The word
    ``Viz`` is still all over the file, so every source-level assertion kept
    passing while the dot lane threw ``TypeError: Viz is not a constructor`` on
    every diagram (mutation M16 — nothing in the suite noticed).
  * **a missing sanitiser.** Dropping ``DOMPurify.sanitize`` from the dot lane
    leaves raw Graphviz SVG assigned to innerHTML — an XSS sink. The identifier
    ``DOMPurify`` still appears three lines above, in the comment that explains
    why it must be there (mutation M53 — nothing noticed).
  * **broken JavaScript.** ``Object.keys(RICH_LANES)`` only answers if the object
    parses. A text search does not care whether the file still runs (M60).

Everything here is offline: node, no network, no browser, no dev server.
"""
import json
import pathlib
import re
import shutil
import subprocess

from _jsrun import run_node

import pytest

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _rich_lanes_src(html: str) -> str:
    """The RICH_LANES object literal, bounded by its own closing brace."""
    start = html.index("const RICH_LANES={")
    return html[start:html.index("\n};\n", start) + 3]


def _lane_block(html: str, name: str) -> str:
    """One lane's entry, from its key to the next lane's key."""
    obj = _rich_lanes_src(html)
    keys = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ([A-Za-z0-9_]+):\{", obj)]
    for i, (pos, k) in enumerate(keys):
        if k == name:
            return obj[pos:keys[i + 1][0] if i + 1 < len(keys) else len(obj)]
    raise AssertionError(f"{name} is not a key of RICH_LANES")


def _js_fn(html: str, header: str) -> str:
    start = html.index(header)
    return html[start:html.index("\n}", start) + 2]


# Stand-ins for the browser globals the lanes touch. `Viz` is deliberately a plain
# namespace OBJECT — exactly what @viz-js/viz 3.x exposes — so `new Viz()` throws
# here for the same reason it throws in a real browser.
_STUBS = """
let sanitizeCalls=0,lastCfg=null;
const DOMPurify={sanitize(x,cfg){sanitizeCalls++;lastCfg=cfg;
  return String(x).replace(/javascript:[^"']*/g,'');}};
const window={DOMPurify};
const Viz={instance:async()=>({renderSVGElement(src){return{
  outerHTML:'<svg data-dot="'+src+'"><a xlink:href="javascript:alert(1)">n</a></svg>',
  removeAttribute(){},style:{}};}})};
// Real CDN base so a recorded load URL is a full, checkable URL. _loadScript /
// _loadCss RECORD the assets each lane's load() asks for — see
// test_every_lane_has_a_callable_draw, which runs load() instead of grepping it.
const CDN='https://cdnjs.cloudflare.com/ajax/libs/';
const asked=[];
const _loadScript=u=>{asked.push(u);return Promise.resolve();};
const _loadCss=u=>{asked.push(u);return Promise.resolve();};
"""


def _node(js: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not available")
    r = run_node(js, timeout=90)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _viz_prelude(html: str) -> str:
    """The cached Viz instance promise the dot lane calls."""
    start = html.index("let _vizP=null;")
    return html[start:html.index("\n", html.index("const _vizInst=", start))]


def test_dot_lane_uses_the_viz3_instance_api():
    """viz-js 3.x has no `new Viz()`: the global is a namespace and instance()
    returns a Promise. The 1.5.0 upgrade was exactly this change, and nothing in
    the suite guarded it — reverting the one line (mutation M16) left the whole
    lane throwing on every ```dot block with the suite green.
    """
    html = _html()
    js = (_STUBS + _viz_prelude(html) + "\nconst LANE={\n" + _lane_block(html, "dot") + "\n};\n" + """
(async()=>{let err=null;const out={innerHTML:'',clientWidth:600};
 try{await LANE.dot.draw(out,'digraph{rankdir=LR;a->b;b->c;}','u1');}
 catch(e){err=String((e&&e.message)||e);}
 console.log(JSON.stringify({err,html:out.innerHTML,sanitizeCalls,lastCfg}));})();
""")
    res = _node(js)
    assert res["err"] is None, f"the dot lane threw: {res['err']}"
    assert "data-dot=" in res["html"] and "a-&gt;b" not in res["html"], \
        f"nothing was rendered into the card: {res['html']!r}"
    assert "rankdir=LR" in res["html"], f"the DOT source never reached the renderer: {res['html']!r}"
    assert res["sanitizeCalls"] == 1, "Viz output was not passed through the sanitiser"


def test_dot_lane_sanitises_graphviz_output():
    """Graphviz passes `javascript:` URLs through verbatim, so the DOMPurify pass
    is the layer that strips them. Removing it (mutation M53) left an XSS sink
    with no test of any kind — the word DOMPurify survives in the comment above.
    """
    html = _html()
    js = (_STUBS + _viz_prelude(html) + "\nconst LANE={\n" + _lane_block(html, "dot") + "\n};\n" + """
(async()=>{let err=null;const out={innerHTML:'',clientWidth:600};
 try{await LANE.dot.draw(out,'digraph{a[URL="javascript:alert(1)"];a->b;}','u2');}
 catch(e){err=String((e&&e.message)||e);}
 console.log(JSON.stringify({err,html:out.innerHTML,sanitizeCalls,lastCfg}));})();
""")
    res = _node(js)
    assert res["err"] is None, f"the dot lane threw: {res['err']}"
    assert res["sanitizeCalls"] == 1, \
        f"DOMPurify.sanitize ran {res['sanitizeCalls']} times — Viz output reached innerHTML raw"
    assert res["lastCfg"] == {"USE_PROFILES": {"svg": True, "svgFilters": True}}, res["lastCfg"]
    assert "javascript:" not in res["html"], \
        f"a javascript: URL survived into the card: {res['html']!r}"


def test_lane_lookup_rejects_prototype_keys():
    """```__proto__ or ```constructor as a fence language would pass a plain
    `RICH_LANES[k]` truthiness check and be dispatched as a renderer. Reverting
    the own-property guard (mutation M54) had no test at all.
    """
    html = _html()
    js = ("const RICH_LANES={dot:{label:'x'},mermaid:{label:'y'}};\n"
          + _js_fn(html, "function _lane(kind){") + """
console.log(JSON.stringify({proto:_lane('__proto__')===null,ctor:_lane('constructor')===null,
 toStr:_lane('toString')===null,valueOf:_lane('valueOf')===null,real:_lane('dot')!==null}));
""")
    res = _node(js)
    for key in ("proto", "ctor", "toStr", "valueOf"):
        assert res[key] is True, f"_lane() dispatched an inherited key ({key}) as a renderer"
    assert res["real"] is True, "_lane() no longer resolves a real lane"


def test_rich_lanes_parses_and_holds_every_lane():
    """Parse RICH_LANES under node and read the keys back.

    ``f"\\n  {lane}:{{" in html[html.index("const RICH_LANES={"):]`` sliced to end
    of file, so renaming the real lane and pasting the key — in syntactically
    invalid JavaScript — further down satisfied it (mutation M60). Object.keys()
    can only answer if the object actually parses.
    """
    html = _html()
    js = (_STUBS + _viz_prelude(html) + "\n" + _rich_lanes_src(html)
          + "\nconsole.log(JSON.stringify(Object.keys(RICH_LANES)));")
    keys = _node(js)
    expected = ["mermaid", "chart", "gantt", "timeline", "network", "geojson", "dot",
                "geometry", "map", "plot3d", "calc", "solve", "stats", "truth",
                "table", "diff", "regex", "molecule", "molecule3d"]
    assert keys == expected, f"RICH_LANES changed shape: {keys}"


@pytest.mark.parametrize("lane", ["mermaid", "chart", "gantt", "timeline", "network",
                                  "geojson", "dot", "geometry", "map", "plot3d",
                                  "calc", "solve", "stats", "truth", "table", "diff",
                                  "regex", "molecule", "molecule3d"])
def test_every_lane_has_a_callable_draw(lane):
    """A lane whose draw is missing or not a function renders an empty card — and a
    lane whose load() throws or fetches an off-CDN asset silently fails to render.

    The old test only checked ``typeof``; 13 of its 19 cases never failed anywhere in
    the sweep. This RUNS the lane's load() under recording stubs (gantt/timeline
    delegate to mermaid, geojson to map — all three delegations are exercised) and
    asserts it resolves and asks only for real CDN .js/.css assets. That catches a
    runtime error inside load() — an undefined helper, a bad await — which a source
    grep for `_loadScript('literal')` cannot see.
    """
    html = _html()
    js = (_STUBS + _viz_prelude(html) + "\n" + _rich_lanes_src(html) + f"""
const L=RICH_LANES[{lane!r}];
(async()=>{{
  let err=null; asked.length=0;
  try{{ await (L.load?L.load():Promise.resolve()); }}
  catch(e){{ err=String((e&&e.message)||e); }}
  console.log(JSON.stringify({{draw:typeof L.draw,label:typeof L.label,err,asked:[...asked]}}));
}})();
""")
    res = _node(js)
    assert res["draw"] == "function", f"{lane}.draw is {res['draw']}"
    assert res["label"] == "string", f"{lane} has no label"
    assert res["err"] is None, f"{lane}.load() threw at runtime: {res['err']}"
    for u in res["asked"]:
        assert u.startswith(("https://cdnjs.cloudflare.com/", "https://cdn.jsdelivr.net/")), \
            f"{lane}.load() fetches an off-CDN asset: {u}"
        assert u.endswith((".js", ".css")), f"{lane}.load() fetches a non-asset URL: {u}"
