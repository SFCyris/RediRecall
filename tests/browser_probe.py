#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the real chart card in a real browser and print measurements as JSON.

This file is NOT collected by pytest (the name does not start with ``test_``).
``tests/test_chart_browser.py`` runs it as a subprocess under whichever
interpreter has Playwright installed, because the project venv does not.

Why a browser at all, when every other frontend test runs under node: the chart
zoom defects are only observable through real input and real layout.

  * a wheel notch only zooms if it reaches chartjs-plugin-zoom's own listener as
    a genuine ``WheelEvent`` with ``ctrlKey`` set, and only then does the axis
    range move at all;
  * "the reset button is hidden" is ``offsetHeight === 0`` — a property that
    needs CSS, a layout pass and the real stylesheet, none of which node has.

Nothing is mocked except the four page-level helpers the delegated click handler
calls but this card does not depend on (``enhanceTables``, ``toast`` …). The
chart lane, the card markup, the click handler, ``_loadScript``, the CDN base and
the whole stylesheet are all extracted verbatim out of ``redirecall/index.html``,
so a change to any of them changes what is measured here.

The three CDN scripts are served from ``~/.cache/redirecall-test-assets`` (see
ASSET_DIR) and downloaded once on first run, so the sweep can run offline and
does not hammer cdnjs 80+ times.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.request

CDN_PREFIX = "https://cdnjs.cloudflare.com/ajax/libs/"
ASSET_DIR = pathlib.Path(
    os.environ.get("REDIRECALL_TEST_ASSET_DIR",
                  pathlib.Path.home() / ".cache" / "redirecall-test-assets"))


# ── extraction out of index.html ─────────────────────────────────────────────
def _rich_lanes_src(html: str) -> str:
    start = html.index("const RICH_LANES={")
    return html[start:html.index("\n};\n", start) + 3]


def _lane_block(html: str, name: str) -> str:
    obj = _rich_lanes_src(html)
    keys = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ([A-Za-z0-9_]+):\{", obj)]
    for i, (pos, k) in enumerate(keys):
        if k == name:
            return obj[pos:keys[i + 1][0] if i + 1 < len(keys) else len(obj)]
    raise AssertionError(f"{name} is not a key of RICH_LANES")


def _js_fn(html: str, header: str) -> str:
    start = html.index(header)
    return html[start:html.index("\n}", start) + 2]


def _between(html: str, start_marker: str, end_marker: str) -> str:
    start = html.index(start_marker)
    end = html.index(end_marker, start) + len(end_marker)
    return html[start:end]


def build_harness(html: str) -> str:
    """A page holding the real card, the real lane and the real click handler."""
    style = _between(html, "<style>", "</style>")
    style = style[len("<style>"):-len("</style>")]

    cdn_line = _between(html, "const CDN='", "';")
    load_script = _js_fn(html, "function _loadScript(url){")
    esc_html = _between(html, "function escHtml(s){", "}\n")
    chart_lane = _lane_block(html, "chart")
    # The card markup, straight out of the marked `code` renderer: `style`,
    # `pngBtn` and the .rich-wrap template. Its free variables become the
    # parameters of cardHtml() below.
    card_tpl = _between(html, "          const style=lane.fixed?", "</div>`;")
    click_handler = _between(html, "document.addEventListener('click',function(e){", "\n});\n")
    # ⛶ Maximize moves .rich-output out of the card and into an overlay, which is
    # exactly the case a canvas-relative lookup for the reset button gets wrong,
    # so the real functions and the real overlay markup are used, not a mock.
    overlay = _between(html, '<div id="viz-max-overlay"', "</div>\n</div>\n")
    viz_resize = _js_fn(html, "function _vizResize(outEl){")
    # openMaximize/closeMaximize call the maximized pan/zoom helpers — extract the
    # whole block (const _PZ_KINDS … _pzDisable) or the maximize flow throws
    # ReferenceError the moment a test maximizes a card.
    pan_zoom = _between(html, "const _PZ_KINDS", "outEl._pz=null;\n}")
    open_max = _js_fn(html, "function openMaximize(btn){")
    close_max = _js_fn(html, "function closeMaximize(){")
    vm_key = _between(html, "function _vmKeyClose(e){", "}\n")

    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>\n" + style +
        "\n</style></head><body><div id='col' style='width:720px'>"
        "<div id='host'></div></div>\n" + overlay + "\n<script>\n"
        # page globals the extracted code closes over
        + cdn_line + "\n"
        "const _libP={};\n"
        + load_script + "\n"
        + esc_html + "\n"
        # stubs: called by the click handler, not part of what is measured
        "window.__enhanceCalls=0;\n"
        "function enhanceTables(root){window.__enhanceCalls++;}\n"
        "function toast(){}\n"
        "function _abcPlay(){}\nfunction downloadSvgAsPng(){}\n"
        "function downloadImgFromSrc(){}\nfunction openLightbox(){}\n"
        "function _isDark(){return false;}\n"
        # the real maximize flow (incl. the pan/zoom helpers it references)
        + viz_resize + "\n" + pan_zoom + "\n" + open_max + "\n" + close_max + "\n"
        + vm_key + "\n"
        # the real lane and the real card markup
        "const RICH_LANES={\n" + chart_lane + "\n};\n"
        "function cardHtml(laneKey,lane,esc){\n" + card_tpl + "\n}\n"
        # the real delegated click handler
        + click_handler + "\n"
        "window.__rr={\n"
        "  async build(src){\n"
        "    const host=document.getElementById('host');\n"
        "    host.innerHTML=cardHtml('chart',RICH_LANES.chart,'');\n"
        "    const wrap=host.querySelector('.rich-wrap');\n"
        "    const out=wrap.querySelector('.rich-output');\n"
        "    await RICH_LANES.chart.load();\n"
        "    await RICH_LANES.chart.draw(out,src);\n"
        "    window.__wrap=wrap; window.__out=out;\n"
        "    window.__chart=out._richInst&&out._richInst.obj;\n"
        "    return !!window.__chart;\n"
        "  },\n"
        "  scales(){const c=window.__chart;\n"
        "    const g=k=>c.scales[k]?[c.scales[k].min,c.scales[k].max]:null;\n"
        "    const t=k=>c.scales[k]?(c.scales[k].ticks||[]).length:null;\n"
        "    return {x:g('x'),y:g('y'),xticks:t('x'),yticks:t('y'),\n"
        "            level:c.getZoomLevel?c.getZoomLevel():null};},\n"
        # painted pixels inside the plot area: 'the chart went blank' is a pixel
        # claim, not a data claim.
        "  ink(){const c=window.__chart, cv=c.canvas, a=c.chartArea;\n"
        "    const dpr=cv.width/cv.clientWidth;\n"
        "    const x=Math.round(a.left*dpr), y=Math.round(a.top*dpr);\n"
        "    const w=Math.round((a.right-a.left)*dpr), h=Math.round((a.bottom-a.top)*dpr);\n"
        "    const d=cv.getContext('2d').getImageData(x,y,w,h).data;\n"
        "    let n=0; for(let i=3;i<d.length;i+=4){if(d[i]>8)n++;}\n"
        "    return {ink:n,total:w*h,frac:+(n/(w*h)).toFixed(5)};},\n"
        "  btn(sel){const b=window.__wrap.querySelector(sel); if(!b)return null;\n"
        "    const cs=getComputedStyle(b), r=b.getBoundingClientRect();\n"
        "    return {h:b.offsetHeight,w:b.offsetWidth,display:cs.display,\n"
        "            visibility:cs.visibility,rect:[r.width,r.height],\n"
        "            text:(b.textContent||'').trim()};},\n"
        "  canvasBox(){const cv=window.__out.querySelector('canvas');\n"
        "    if(!cv)return null; const r=cv.getBoundingClientRect();\n"
        "    return {x:r.x,y:r.y,w:r.width,h:r.height};},\n"
        "  table(){const t=window.__wrap.querySelector('.rr-chart-data table');\n"
        "    if(!t)return null;\n"
        "    return {head:[...t.querySelectorAll('thead th')].map(e=>e.textContent),\n"
        "            rows:[...t.querySelectorAll('tbody tr')].map(\n"
        "              tr=>[...tr.querySelectorAll('td')].map(e=>e.textContent))};},\n"
        "};\n</script></body></html>\n")


# ── CDN assets, cached on disk ───────────────────────────────────────────────
def _asset(url: str) -> bytes | None:
    if not url.startswith(CDN_PREFIX):
        return None
    name = url[len(CDN_PREFIX):].split("?")[0].replace("/", "_")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    path = ASSET_DIR / name
    if path.exists():
        return path.read_bytes()
    try:                                        # first run only; then offline
        with urllib.request.urlopen(url, timeout=20) as r:
            if r.status != 200:
                return None
            blob = r.read()
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return blob
    except Exception:
        return None


# ── the run ──────────────────────────────────────────────────────────────────
LINE_CFG = json.dumps({"type": "line", "data": {
    "labels": ["A", "B", "C", "D", "E"],
    "datasets": [{"label": "v", "data": [3, 1, 4, 1, 5]}]}})

SCATTER_CFG = json.dumps({"type": "scatter", "data": {"datasets": [
    {"label": "obs", "data": [{"x": 1, "y": 2}, {"x": 2, "y": 4},
                              {"x": 3, "y": 9}, {"x": 4, "y": 16}]},
    {"label": "ref", "data": [{"x": 1, "y": 1}, {"x": 4, "y": 4}]}]}})

BUBBLE_CFG = json.dumps({"type": "bubble", "data": {"datasets": [
    {"label": "cities", "data": [{"x": 13.4, "y": 52.5, "r": 12},
                                 {"x": 2.35, "y": 48.9, "r": 9}]}]}})


def _wheel(page, box, notches, delta, settle=8):
    """Real ctrl+wheel over the middle of the plot area.

    ``mouse.wheel`` dispatches a genuine ``WheelEvent``; Control is held down so
    it carries ``ctrlKey`` — the plugin is configured ``modifierKey:'ctrl'`` and
    ignores a wheel without it.
    """
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    page.mouse.move(cx, cy)
    page.keyboard.down("Control")
    for _ in range(notches):
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(settle)
    page.keyboard.up("Control")
    page.wait_for_timeout(60)


def _wheel_in(page, box, notches, settle=30):
    _wheel(page, box, notches, -120, settle)


def _wheel_out(page, box, notches, settle=30):
    _wheel(page, box, notches, 120, settle)


def _click(page, sel):
    """A genuine user click, with the actionability checks left ON.

    A button with ``display:none`` cannot be clicked by a user and must not be
    clickable here either — so the failure is recorded as data instead of being
    raised, and the test asserts on ``clicked``.
    """
    try:
        page.click(sel, timeout=2500)
        page.wait_for_timeout(80)
        return {"clicked": True}
    except Exception as e:
        return {"clicked": False, "error": str(e).splitlines()[0][:200]}


def _drag(page, box, dx, dy):
    """Real press-move-release drag across the plot area."""
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + dx, cy + dy, steps=12)
    page.mouse.up()
    page.wait_for_timeout(80)


def run(index_html: pathlib.Path) -> dict:
    from playwright.sync_api import sync_playwright

    harness = build_harness(index_html.read_text(encoding="utf-8"))
    out: dict = {"ok": False}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        # Errors only. Chromium logs a Canvas2D willReadFrequently *warning*
        # because ink() calls getImageData; that is this file's doing, not the
        # page's, and must not be reported as a page error.
        console: list[str] = []
        page.on("console",
                lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        def route(r):
            if r.request.url.endswith("/harness.html"):
                return r.fulfill(status=200, content_type="text/html", body=harness)
            blob = _asset(r.request.url)
            if blob is None:
                return r.abort()
            return r.fulfill(status=200, content_type="application/javascript", body=blob)

        page.route("**/*", route)
        page.goto("https://rr-chart.test/harness.html")

        # ── 1. category line chart: wheel zoom ───────────────────────────────
        built = page.evaluate("s=>window.__rr.build(s)", LINE_CFG)
        line: dict = {"built": built}
        if built:
            box = page.evaluate("()=>window.__rr.canvasBox()")
            line["canvas"] = box
            line["before"] = page.evaluate("()=>window.__rr.scales()")
            line["ink_before"] = page.evaluate("()=>window.__rr.ink()")
            line["btn_before"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            _wheel_in(page, box, 6)
            line["after_zoom"] = page.evaluate("()=>window.__rr.scales()")
            line["ink_after_zoom"] = page.evaluate("()=>window.__rr.ink()")
            line["btn_after_zoom"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            # zooming back out must recover the full range — the trap is that a
            # zero-width range multiplied by the zoom-out factor stays zero.
            _wheel_out(page, box, 12)
            line["after_zoom_out"] = page.evaluate("()=>window.__rr.scales()")
            # a real click on the real button, through the delegated handler
            line["reset_click"] = _click(page, '[data-act="chart-reset"]')
            line["after_reset"] = page.evaluate("()=>window.__rr.scales()")
            line["btn_after_reset"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            _click(page, '[data-act="chart-data"]')
            line["table"] = page.evaluate("()=>window.__rr.table()")
        out["line"] = line

        # ── 2. numeric scatter: drag-pan, then the data table ────────────────
        built = page.evaluate("s=>window.__rr.build(s)", SCATTER_CFG)
        sc: dict = {"built": built}
        if built:
            box = page.evaluate("()=>window.__rr.canvasBox()")
            sc["before"] = page.evaluate("()=>window.__rr.scales()")
            sc["btn_before"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            _drag(page, box, 160, 0)
            sc["after_pan"] = page.evaluate("()=>window.__rr.scales()")
            sc["btn_after_pan"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            sc["reset_click"] = _click(page, '[data-act="chart-reset"]')
            sc["after_reset"] = page.evaluate("()=>window.__rr.scales()")
            # A linear axis shrinks ~10% per notch, so it needs far more notches
            # than a category one to reach its floor. Two passes: the second must
            # move NOTHING if a floor exists, and that comparison does not depend
            # on how many notches the browser actually delivered — an exponential
            # shrink with no floor keeps shrinking no matter where it started.
            _wheel_in(page, box, 200, settle=3)
            sc["after_deep_zoom"] = page.evaluate("()=>window.__rr.scales()")
            _wheel_in(page, box, 20, settle=3)
            sc["after_deeper_zoom"] = page.evaluate("()=>window.__rr.scales()")
            _click(page, '[data-act="chart-reset"]')
            _click(page, '[data-act="chart-data"]')
            sc["table"] = page.evaluate("()=>window.__rr.table()")
        out["scatter"] = sc

        # ── 3. bubble: the data table carries the radius ─────────────────────
        built = page.evaluate("s=>window.__rr.build(s)", BUBBLE_CFG)
        bu: dict = {"built": built}
        if built:
            _click(page, '[data-act="chart-data"]')
            bu["table"] = page.evaluate("()=>window.__rr.table()")
        out["bubble"] = bu

        # ── 4. maximize → zoom in the overlay → restore → reset ──────────────
        built = page.evaluate("s=>window.__rr.build(s)", LINE_CFG)
        mx: dict = {"built": built}
        if built:
            mx["before"] = page.evaluate("()=>window.__rr.scales()")
            mx["max_click"] = _click(page, '[data-act="viz-max"]')
            page.wait_for_timeout(200)          # openMaximize resizes on a timer
            mx["maximized"] = page.evaluate(
                "()=>({inOverlay:!!document.querySelector('#viz-max-content .rich-output'),"
                "      inCard:!!document.querySelector('.rich-wrap .rich-output')})")
            box = page.evaluate("()=>window.__rr.canvasBox()")
            mx["canvas"] = box
            _wheel_in(page, box, 4)
            mx["after_zoom"] = page.evaluate("()=>window.__rr.scales()")
            # ✕ Restore lives in the overlay header, on top of everything. Scope
            # the selector to the overlay: openMaximize also stamps
            # data-act="viz-restore" on the placeholder it leaves in the card,
            # and that one sits behind the full-screen overlay.
            mx["restore_click"] = _click(page, '#viz-max-overlay [data-act="viz-restore"]')
            page.wait_for_timeout(200)
            mx["restored"] = page.evaluate(
                "()=>({inOverlay:!!document.querySelector('#viz-max-content .rich-output'),"
                "      inCard:!!document.querySelector('.rich-wrap .rich-output')})")
            mx["btn_after_restore"] = page.evaluate(
                "()=>window.__rr.btn('[data-act=\"chart-reset\"]')")
            mx["reset_click"] = _click(page, '[data-act="chart-reset"]')
            mx["after_reset"] = page.evaluate("()=>window.__rr.scales()")
        out["maximize"] = mx

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
