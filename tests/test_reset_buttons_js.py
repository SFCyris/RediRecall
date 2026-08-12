# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two 'undo' controls added to the rich-card chrome, run as real JavaScript:

* Editable-source ``↺ Reset`` — a card whose Source has been edited via Apply had
  no way back to the model-authored version except reloading the page. The
  pristine text is snapshotted into `code._originalSrc` the moment the Apply bar
  is first built (before contenteditable is turned on, so it is guaranteed
  unedited), and Reset restores it and re-renders.
* Fractal ``⟲ Reset zoom`` — the escape-time click-to-zoom navigation had no way
  back to the spec's own view, mirroring the chart lane's existing zoom-reset.

Static-source checks only (button creation, snapshot timing, view math); the
full click round-trip through a real canvas is exercised in the browser and was
verified manually — these guard the logic that round-trip depends on.
"""
import pathlib
import re

import pytest

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def test_apply_bar_snapshots_source_before_making_it_editable():
    html = _html()
    i = html.index("code._originalSrc=code.textContent;")
    j = html.index("setAttribute('contenteditable'", i)
    assert i < j, "the snapshot must happen BEFORE the block becomes editable"


def test_reset_button_exists_next_to_apply_in_the_same_bar():
    html = _html()
    bar = html[html.index("bar.className='rich-apply-bar';"):
                html.index("pre.insertBefore(bar,pre.firstChild);")]
    assert "dataset.act='rich-apply'" in bar
    assert "dataset.act='rich-reset'" in bar
    assert "bar.appendChild(apply); bar.appendChild(reset);" in bar


def test_reset_handler_only_acts_when_something_was_actually_captured():
    html = _html()
    i = html.index("}else if(act==='rich-reset'){")
    block = html[i:html.index("\n  }", i)]
    assert "code._originalSrc===undefined" in block, \
        "must no-op before Source has ever been opened (nothing captured yet)"
    assert "code.textContent===code._originalSrc" in block, \
        "must short-circuit when there is nothing to undo"
    assert "_rerenderRichCard(wrap)" in block


def test_apply_bar_css_has_a_gap_between_the_two_buttons():
    html = _html()
    rule = re.search(r"\.rich-apply-bar\{[^}]*\}", html).group(0)
    assert "gap:" in rule, "Apply and Reset would visually touch without a gap"


# ── fractal view reset ────────────────────────────────────────────────────────
def test_fractal_reset_button_ships_hidden_like_charts_does():
    html = _html()
    btn = html[html.index("laneKey==='fractal' ?"):html.index("laneKey==='plot3d' ?")]
    assert 'data-act="fractal-reset"' in btn and 'style="display:none"' in btn


def test_fractal_view_reset_restores_the_specs_own_center_and_zoom():
    html = _html()
    i = html.index("const origView=")
    block = html[i:html.index("out._richInst=inst;", i)]
    assert "resetView(){ inst.view={...origView}; inst.redraw(); }" in block
    assert "view:{...origView}" in block, \
        "the live view must start as a COPY of origView, not the same object " \
        "(else zooming would mutate origView too and Reset would restore nothing)"


def test_click_zoom_reveals_the_reset_button():
    html = _html()
    i = html.index("canvas.addEventListener('click',e=>{")
    block = html[i:html.index("\n        });", i)]
    assert 'querySelector(\'[data-act="fractal-reset"]\')' in block
    assert "b.style.display=''" in block


def test_reset_zoom_handler_hides_the_button_again():
    html = _html()
    i = html.index("}else if(act==='fractal-reset'){")
    block = html[i:html.index("\n  }", i)]
    assert "inst.resetView()" in block and "btn.style.display='none'" in block


def test_rerender_hides_any_leftover_reset_zoom_button():
    """After Apply/source-Reset builds a brand-new fractal instance (its own
    fresh view), a reset-zoom button left visible from the OLD instance must not
    linger — clicking it would be a confusing no-op on state that no longer
    corresponds to what's on screen."""
    html = _html()
    i = html.index("function _rerenderRichCard(wrap){")
    block = html[i:html.index("\n}", i)]
    assert 'data-act="fractal-reset"' in block
    assert "display:none" in block
