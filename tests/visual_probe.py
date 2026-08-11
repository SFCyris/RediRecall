#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the molecule, geometry and abc lanes in a real browser; print JSON measurements.

Companion to ``browser_probe.py`` (which covers the chart lane). Three defects here
are only observable through real layout, real computed colour and the real abcjs
synth, none of which node has:

  * **B4** the molecule card clips ~40% of the structure — a pixel-layout fact
    (``scrollHeight`` > ``clientHeight``), invisible to a source grep.
  * **B5** the geometry lane paints black axis/labels on the dark card (~2:1, below
    WCAG AA). Needs the real stylesheet, the dark theme variables and JSXGraph's
    computed ``fill`` to measure a contrast ratio.
  * **B8** the abc Play button never resets, because ``visualObj.getTotalTime()``
    returns ``null`` and the reset timer got ms=0. Needs the real abcjs synth, whose
    ``duration`` (seconds) is the value the fix must use instead.

Everything measured is extracted verbatim from ``redirecall/index.html`` — the two
lanes, the geometry helpers, the card markup, the loaders, the whole stylesheet and
the theme variables — so a change to any of them changes what is measured. Library
bundles are served from ``~/.cache/redirecall-test-assets`` (downloaded once, then
offline), same as ``browser_probe.py``.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.request

ASSET_DIR = pathlib.Path(
    os.environ.get("REDIRECALL_TEST_ASSET_DIR",
                   pathlib.Path.home() / ".cache" / "redirecall-test-assets"))

ABCJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.6.4/abcjs-basic-min.min.js"
SMILES_URL = "https://cdn.jsdelivr.net/npm/smiles-drawer@2.4.1/dist/smiles-drawer.min.js"
JXG_JS_URL = "https://cdnjs.cloudflare.com/ajax/libs/jsxgraph/1.12.2/jsxgraphcore.js"
JXG_CSS_URL = "https://cdnjs.cloudflare.com/ajax/libs/jsxgraph/1.12.2/jsxgraph.css"


# ── extraction out of index.html ─────────────────────────────────────────────
def _between(html, a, b):
    s = html.index(a); e = html.index(b, s) + len(b); return html[s:e]


def _js_fn(html, header):
    s = html.index(header); return html[s:html.index("\n}", s) + 2]


def _lane(html, name):
    start = html.index("const RICH_LANES={")
    obj = html[start:html.index("\n};\n", start) + 3]
    keys = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ([A-Za-z0-9_]+):\{", obj)]
    for i, (pos, k) in enumerate(keys):
        if k == name:
            return obj[pos:keys[i + 1][0] if i + 1 < len(keys) else len(obj)]
    raise AssertionError(f"{name} is not a key of RICH_LANES")


def _opt_fn(html, header):
    """A function body if present, else '' — the abc duration helper does not exist
    on the unfixed tree, and the probe must run against both."""
    try:
        return _js_fn(html, header)
    except ValueError:
        return ""


def build_harness(html: str) -> str:
    style = _between(html, "<style>", "</style>")[len("<style>"):-len("</style>")]
    vars_root = _between(html, ":root{", "}")
    vars_dark = _between(html, '[data-theme="dark"]{', "}")
    cdn_line = _between(html, "const CDN='", "';")
    load_script = _js_fn(html, "function _loadScript(url){")
    load_css = _js_fn(html, "function _loadCss(url){")
    esc_html = _between(html, "function escHtml(s){", "}\n")
    geo_consts = html[html.index("const _GEO_TYPES=new Set"):html.index("function _solveRoots")]
    molecule = _lane(html, "molecule")
    geometry = _lane(html, "geometry")
    abc_dur = _opt_fn(html, "function _abcDurationMs(")
    card_tpl = _between(html, "          const style=lane.fixed?", "</div>`;")

    return (
        "<!doctype html><html><head><meta charset='utf-8'>\n"
        "<style>:root{" + vars_root + "}\n"
        '[data-theme="dark"]{' + vars_dark + "}\n" + style + "</style>\n"
        f"<script src='{ABCJS_URL}'></script>\n"
        "</head><body>\n"
        # a realistic ancestor chain: the card renders inside an assistant bubble,
        # so the composited dark background matches the app (~rgb(63,63,71)).
        "<div id='chat' style='padding:20px;max-width:900px'>"
        "<div class='msg-bubble ai' style='padding:14px;border-radius:12px'>"
        "<div id='host'></div></div></div>\n"
        "<div id='abc-host'></div>\n"
        "<script>\n"
        "let _libP={};\n"
        + cdn_line + "\n"
        + load_script + "\n" + load_css + "\n" + esc_html + "\n"
        "function _isDark(){return document.documentElement.getAttribute('data-theme')==='dark';}\n"
        + geo_consts + "\n"
        + abc_dur + "\n"
        "const RICH_LANES={\n" + molecule + "\n" + geometry + "\n};\n"
        "function cardHtml(laneKey,lane,esc){\n" + card_tpl + "\n}\n"
        # ── colour maths, all in-page so the numbers come from getComputedStyle ──
        "function _parse(c){const m=String(c).match(/rgba?\\(([^)]+)\\)/);if(!m)return null;"
        "const p=m[1].split(',').map(s=>parseFloat(s.trim()));"
        "return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};}\n"
        "function _over(f,b){const a=f.a;return {r:f.r*a+b.r*(1-a),g:f.g*a+b.g*(1-a),b:f.b*a+b.b*(1-a),a:1};}\n"
        "function _bgOf(el){const body=_parse(getComputedStyle(document.body).backgroundColor)||{r:10,g:10,b:15,a:1};"
        "let base={r:body.r,g:body.g,b:body.b,a:1};const chain=[];let n=el;"
        "while(n&&n!==document.documentElement){chain.push(n);n=n.parentElement;}chain.reverse();"
        "for(const nd of chain){const c=_parse(getComputedStyle(nd).backgroundColor);if(c&&c.a>0)base=_over(c,base);}return base;}\n"
        "function _lin(c){c/=255;return c<=0.03928?c/12.92:Math.pow((c+0.055)/1.055,2.4);}\n"
        "function _L(col){return 0.2126*_lin(col.r)+0.7152*_lin(col.g)+0.0722*_lin(col.b);}\n"
        "function _contrast(a,b){const la=_L(a),lb=_L(b);const hi=Math.max(la,lb),lo=Math.min(la,lb);return (hi+0.05)/(lo+0.05);}\n"
        "window.__mol=async function(smiles){\n"
        "  const host=document.getElementById('host');\n"
        "  host.innerHTML=cardHtml('molecule',RICH_LANES.molecule,'');\n"
        "  const wrap=host.querySelector('.rich-wrap'), out=wrap.querySelector('.rich-output');\n"
        "  await RICH_LANES.molecule.load();\n"
        "  try{ await RICH_LANES.molecule.draw(out,smiles,'m1'); }catch(e){ return {built:false,error:String(e)}; }\n"
        "  const svg=out.querySelector('svg'); const r=svg?svg.getBoundingClientRect():null;\n"
        "  return {built:!!svg, clientH:out.clientHeight, scrollH:out.scrollHeight,\n"
        "          clientW:out.clientWidth, scrollW:out.scrollWidth,\n"
        "          svgW:r?Math.round(r.width):null, svgH:r?Math.round(r.height):null};\n"
        "};\n"
        "window.__geo=async function(spec){\n"
        "  const host=document.getElementById('host');\n"
        "  host.innerHTML=cardHtml('geometry',RICH_LANES.geometry,'');\n"
        "  const wrap=host.querySelector('.rich-wrap'), out=wrap.querySelector('.rich-output');\n"
        "  await RICH_LANES.geometry.load();\n"
        "  try{ await RICH_LANES.geometry.draw(out,spec,'g1'); }catch(e){ return {built:false,error:String(e)}; }\n"
        "  const bg=_bgOf(out);\n"
        "  const texts=[...out.querySelectorAll('text')];\n"
        "  const labels=texts.map(t=>{const cs=getComputedStyle(t);const ink=_parse(cs.fill)||_parse(cs.color);\n"
        "    return {text:(t.textContent||'').trim(), fill:cs.fill, ink,\n"
        "            contrast: ink? +_contrast(ink,bg).toFixed(3):null};}).filter(l=>l.text.length&&l.ink);\n"
        "  const cs=labels.map(l=>l.contrast).filter(x=>x!=null);\n"
        "  const noteEl=out.querySelector('.rich-note');\n"
        "  const nShapes=out.querySelectorAll('svg path, svg ellipse, svg line, svg circle, svg polygon').length;\n"
        "  return {built:!!texts.length, bg, nLabels:labels.length, nShapes,\n"
        "          note: noteEl?noteEl.textContent:null,\n"
        "          noteH: noteEl?Math.round(noteEl.getBoundingClientRect().height):0,\n"
        "          minContrast: cs.length?Math.min(...cs):null, labels:labels.slice(0,40)};\n"
        "};\n"
        "window.__abc=async function(src){\n"
        "  const o={};\n"
        "  o.supportsAudio=!!(window.ABCJS&&ABCJS.synth&&ABCJS.synth.supportsAudio&&ABCJS.synth.supportsAudio());\n"
        "  const host=document.getElementById('abc-host');\n"
        "  const tunes=ABCJS.renderAbc(host, src, {responsive:'resize'});\n"
        "  const tune=Array.isArray(tunes)?tunes[0]:tunes;\n"
        "  o.rendered=!!(host.querySelector('svg'));\n"
        "  const gtt=(tune&&typeof tune.getTotalTime==='function')?tune.getTotalTime():'noFn';\n"
        "  o.getTotalTime=gtt;\n"
        "  const synth=new ABCJS.synth.CreateSynth();\n"
        "  const ac=new (window.AudioContext||window.webkitAudioContext)();\n"
        "  await synth.init({audioContext:ac, visualObj:tune, options:{program:0}});\n"
        "  await synth.prime().catch(()=>null);   // sets synth.duration even if the soundfont fetch is blocked\n"
        "  o.synthDuration=(typeof synth.duration==='number')?synth.duration:null;\n"
        "  // resetMs is what the reset timer is scheduled from: the REAL helper when the\n"
        "  // fix is present, otherwise the old getTotalTime() expression the lane used.\n"
        "  o.resetMs=(typeof _abcDurationMs==='function')\n"
        "    ? _abcDurationMs(synth)\n"
        "    : Math.round(((tune&&tune.getTotalTime&&tune.getTotalTime()||0)*1000)||0);\n"
        "  o.usedHelper=(typeof _abcDurationMs==='function');\n"
        "  return o;\n"
        "};\n"
        "</script></body></html>\n")


# ── assets, cached on disk ───────────────────────────────────────────────────
def _asset(url: str) -> bytes | None:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])
    path = ASSET_DIR / name
    if path.exists():
        return path.read_bytes()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "redirecall-test"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None
            blob = r.read()
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return blob
    except Exception:
        return None


CAFFEINE = "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"
GEO_SPEC = json.dumps({"boundingbox": [-4, 4, 4, -4], "axis": True, "elements": [
    {"type": "point", "args": [1, 2], "attrs": {"name": "A"}},
    {"type": "circle", "args": [[0, 0], 2]},
    {"type": "text", "args": [-3, -3, "note"]}]})
ABC_TUNE = "X:1\nT:Scale\nM:4/4\nL:1/4\nK:C\nC D E F | G A B c |"
# 3 buildable elements (one uses the SVG circle flat form) + 2 unbuildable: an
# unsupported relational type and a non-numeric arg. Expect 3 drawn, 2 noted.
GEO_RESILIENCE = json.dumps({"boundingbox": [-4, 4, 4, -4], "axis": False, "elements": [
    {"type": "text", "args": [0, 3, "ok"]},
    {"type": "segment", "args": [[-3, 0], [3, 0]], "attrs": {"strokeColor": "green"}},
    {"type": "circle", "args": [0, 0, 2]},                    # flat3 -> reshaped, draws
    {"type": "perpendicular", "args": [[0, 0], [3, 0]]},      # unsupported -> skip
    {"type": "ellipse", "args": ["bad"]}]})                   # non-number -> skip
# A QUOTED-number boundingbox (a common model quirk) must be coerced, not thrown on —
# the elements are all fine, so the figure must render rather than blank.
GEO_BADBOX = json.dumps({"boundingbox": ["-4", "4", "4", "-4"], "axis": False, "elements": [
    {"type": "circle", "args": [0, 0, 2]},
    {"type": "text", "args": [0, 3, "ok"]}]})

_CT = {"application/javascript", "text/css"}


def run(index_html: pathlib.Path) -> dict:
    from playwright.sync_api import sync_playwright

    harness = build_harness(index_html.read_text(encoding="utf-8"))
    out: dict = {"ok": False}
    asset_map = {ABCJS_URL: "application/javascript", SMILES_URL: "application/javascript",
                 JXG_JS_URL: "application/javascript", JXG_CSS_URL: "text/css"}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        console: list[str] = []
        # B8 primes the synth with the network cut, so the soundfont sample fetches are
        # aborted by the router and abcjs logs decode/network errors. Those are the
        # probe's own doing, not a page fault; every other request is served from cache,
        # so an aborted request here is always a soundfont sample. Genuine JS errors
        # (pageerror, TypeError, …) are still recorded.
        _benign = ("soundfont", "decode sound", "load note",
                   "err_failed", "failed to load resource")
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" and not any(b in m.text.lower() for b in _benign)
                else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        def route(r):
            url = r.request.url
            if url.endswith("/harness.html"):
                return r.fulfill(status=200, content_type="text/html", body=harness)
            if url in asset_map:
                blob = _asset(url)
                if blob is None:
                    return r.abort()
                return r.fulfill(status=200, content_type=asset_map[url], body=blob)
            return r.abort()   # soundfont samples etc. — prime() rejects, duration still set

        page.route("**/*", route)
        page.goto("https://rr-visual.test/harness.html")

        # B4 — molecule clip (light theme, the default)
        page.evaluate("()=>document.documentElement.removeAttribute('data-theme')")
        out["molecule"] = page.evaluate("s=>window.__mol(s)", CAFFEINE)

        # B5 — geometry contrast in DARK, then a LIGHT render to prove no global leak
        page.evaluate("()=>document.documentElement.setAttribute('data-theme','dark')")
        out["geometry_dark"] = page.evaluate("s=>window.__geo(s)", GEO_SPEC)
        page.evaluate("()=>document.documentElement.removeAttribute('data-theme')")
        out["geometry_light"] = page.evaluate("s=>window.__geo(s)", GEO_SPEC)

        # Resilience — a spec with good elements + a couple that can't be built. The
        # good ones must still draw; the bad ones must be skipped and named in a footnote
        # rather than throwing and blanking the whole card.
        out["geometry_resilience"] = page.evaluate("s=>window.__geo(s)", GEO_RESILIENCE)
        out["geometry_badbox"] = page.evaluate("s=>window.__geo(s)", GEO_BADBOX)

        # B8 — abc reset duration source
        out["abc"] = page.evaluate("s=>window.__abc(s)", ABC_TUNE)

        out["console"] = console
        out["ok"] = True
        browser.close()
    return out


def main() -> int:
    index = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if index is None or not index.exists():
        print(json.dumps({"ok": False, "error": f"no index.html at {index}"}))
        return 1
    try:
        import playwright  # noqa: F401
    except Exception as e:
        print(json.dumps({"skip": f"playwright not importable: {e}"}))
        return 0
    try:
        print(json.dumps(run(index)))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            print(json.dumps({"skip": f"no chromium for playwright: {msg[:200]}"}))
            return 0
        print(json.dumps({"ok": False, "error": msg[:4000]}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
