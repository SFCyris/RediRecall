# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chart zoom and the chart data table, measured in a real browser.

Everything here is driven through real input — a ``WheelEvent`` with ``ctrlKey``
from ``mouse.wheel``, a press-move-release drag, and ordinary clicks with
Playwright's actionability checks left ON — and asserted on measured layout:
axis ranges read off ``chart.scales``, ``offsetHeight`` off the reset button,
painted pixels off the canvas.

That is the point. The two defects these tests cover both passed a suite of
source-level assertions:

  * **A1.** ``limits:{x:{minRange:1e-9}}`` reads like a limit and is not one. On a
    category axis the zoom plugin steps by whole label indices, so five labels
    collapsed from ``[0,4]`` to ``[2,2]`` — zero width — in two wheel notches.
    The "⟲ Reset zoom" button that would have undone it is created with
    ``style="display:none"`` and no ``onZoom``/``onPan`` callback ever unhid it:
    measured ``offsetHeight: 0`` in every state, and a real click timed out. The
    chart was unrecoverable short of reloading the page.
  * **A2.** the data table was built from ``data.labels``, which scatter and
    bubble do not have. Measured ``rows: []`` — an empty table for exactly the
    chart types that get zoom.

The browser work lives in ``tests/browser_probe.py``; it runs once per session
under whichever interpreter has Playwright (the project venv does not), so the
suite still runs on a bare checkout — it skips instead.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "redirecall" / "index.html"
_PROBE = pathlib.Path(__file__).with_name("browser_probe.py")

_CANDIDATE_PYTHONS = [
    os.environ.get("REDIRECALL_TEST_PLAYWRIGHT_PYTHON"),
    sys.executable,
    shutil.which("python3"),
    shutil.which("python"),
    str(pathlib.Path.home() / "miniconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / "anaconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / ".pyenv" / "shims" / "python3"),
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
]
_resolved: list = []

def _no_browser():
    """Skip — or FAIL when CI insists the browser tests must really run.

    A silent skip is indistinguishable from a pass in CI output: the suite would be a
    no-op with nothing saying so. REDIRECALL_REQUIRE_BROWSER_TESTS=1 makes a missing
    interpreter a hard failure, so a pipeline can assert these actually executed.
    """
    msg = ("no interpreter with playwright installed "
           "(set REDIRECALL_TEST_PLAYWRIGHT_PYTHON)")
    if os.environ.get("REDIRECALL_REQUIRE_BROWSER_TESTS") == "1":
        pytest.fail(msg + " — required because REDIRECALL_REQUIRE_BROWSER_TESTS=1")
    pytest.skip(msg)



def _playwright_python():
    """First interpreter on this machine that can import playwright, or None."""
    if _resolved:
        return _resolved[0]
    seen = set()
    for cand in _CANDIDATE_PYTHONS:
        if not cand or cand in seen or not pathlib.Path(cand).exists():
            continue
        seen.add(cand)
        try:
            r = subprocess.run([cand, "-c", "import playwright"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            _resolved.append(cand)
            return cand
    _resolved.append(None)
    return None


@pytest.fixture(scope="session")
def probe():
    """One browser session for the whole module: build, zoom, pan, click, measure."""
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    assert out, f"probe printed nothing (exit {r.returncode}):\n{r.stderr[-2000:]}"
    data = json.loads(out.splitlines()[-1])
    if "skip" in data:
        pytest.skip(data["skip"])
    assert data.get("ok"), f"probe failed: {data.get('error')}"
    assert not data.get("console"), f"the page logged errors: {data['console']}"
    return data


def _width(rng):
    return rng[1] - rng[0]


# ── A1: the zoom trap ────────────────────────────────────────────────────────
def test_issueA1_ctrl_wheel_cannot_collapse_the_category_axis(probe):
    """Six ctrl+wheel notches took the x scale of a five-label line chart from
    [0,4] to [2,2] — a zero-width range showing one label. `minRange:1e-9` never
    applied, because the category zoom path steps by whole indices and a range of
    one index still rounds to a single label.
    """
    line = probe["line"]
    assert line["built"], "the chart never rendered"
    assert _width(line["before"]["x"]) == 4, line["before"]
    after = line["after_zoom"]["x"]
    assert _width(after) > 0, \
        f"the x axis collapsed to a zero-width range {after} — the chart is blank"
    assert _width(after) >= 2, \
        f"fewer than three labels survive a zoom: {after}"
    assert line["after_zoom"]["xticks"] >= 2, \
        f"only {line['after_zoom']['xticks']} tick(s) left on x"
    # the zoom really happened, i.e. the wheel event carried ctrlKey and reached
    # the plugin — otherwise "did not collapse" would be trivially true
    assert _width(after) < _width(line["before"]["x"]), \
        "the ctrl+wheel did not zoom at all; the interaction under test never ran"
    assert line["ink_after_zoom"]["ink"] > 0, "nothing is painted in the plot area"


def test_issueA1_reset_button_becomes_visible_and_clickable_after_a_wheel_zoom(probe):
    """The reset button had `offsetHeight: 0` and `display:none` in every state:
    it is created hidden and nothing ever unhid it (there was no onZoom/onPan
    callback at all). A real click timed out — "Element is not visible" — so a
    zoomed chart could only be restored by reloading the page.
    """
    line = probe["line"]
    assert line["btn_before"]["h"] == 0, \
        "the reset button should start hidden — there is nothing to reset yet"
    btn = line["btn_after_zoom"]
    assert btn["h"] > 0, \
        f"reset button still has offsetHeight {btn['h']} after a zoom: {btn}"
    assert btn["display"] != "none", f"computed display is {btn['display']}"
    assert btn["rect"][0] > 0 and btn["rect"][1] > 0, \
        f"reset button occupies no space: {btn['rect']}"
    assert line["reset_click"]["clicked"], \
        f"a real user click on the reset button failed: {line['reset_click']}"


def test_issueA1_reset_restores_the_original_range(probe):
    """With the button unreachable the view could not be restored: after zooming
    in and back out by wheel the y axis sat at [-0.79, 6.74] against an original
    [1, 5], and the click that would have fixed it never landed.
    """
    line = probe["line"]
    assert line["after_reset"]["x"] == line["before"]["x"], \
        f"x not restored: {line['after_reset']['x']} vs {line['before']['x']}"
    assert line["after_reset"]["y"] == line["before"]["y"], \
        f"y not restored: {line['after_reset']['y']} vs {line['before']['y']}"
    assert line["btn_after_reset"]["h"] == 0, \
        "the reset button stays visible after a reset, with nothing left to reset"


def test_issueA1_drag_pan_reveals_the_reset_button(probe):
    """Pan has always worked — it was the way *back* that did not exist. A drag
    moved the scatter's x window from [1,4] to [0.09,3.09] and left the reset
    button at offsetHeight 0, so the panned-away view was permanent.
    """
    sc = probe["scatter"]
    assert sc["built"], "the scatter chart never rendered"
    assert sc["btn_before"]["h"] == 0, "the button should start hidden"
    assert sc["after_pan"]["x"] != sc["before"]["x"], \
        "the drag did not pan anything; the interaction under test never ran"
    btn = sc["btn_after_pan"]
    assert btn["h"] > 0, f"reset button still hidden after a drag-pan: {btn}"
    assert sc["reset_click"]["clicked"], \
        f"a real user click on the reset button failed: {sc['reset_click']}"
    assert sc["after_reset"]["x"] == sc["before"]["x"], \
        f"x not restored after pan+reset: {sc['after_reset']['x']}"
    assert sc["after_reset"]["y"] == sc["before"]["y"], \
        f"y not restored after pan+reset: {sc['after_reset']['y']}"


def test_issueA1_linear_axis_zoom_stops_at_a_data_derived_floor(probe):
    """`minRange:1e-9` let a linear axis shrink nine orders of magnitude below
    its data: 200 notches on a series spanning x=1..4 left a window 2.1e-9 wide.
    The floor must come from the data (span/1000), and must be a floor — 20 more
    notches past it may not narrow the window any further.
    """
    sc = probe["scatter"]
    deep, deeper = sc["after_deep_zoom"], sc["after_deeper_zoom"]
    wx, wy = _width(deep["x"]), _width(deep["y"])
    # data spans: x 1..4 = 3, y 1..16 = 15  ->  floors 0.003 and 0.015
    assert wx >= 0.0015, f"x window {wx:.3g} is below half the data-derived floor"
    assert wy >= 0.0075, f"y window {wy:.3g} is below half the data-derived floor"
    assert abs(_width(deeper["x"]) - wx) < 1e-9, \
        f"x kept shrinking past the floor: {wx:.17g} -> {_width(deeper['x']):.17g}"
    assert abs(_width(deeper["y"]) - wy) < 1e-9, \
        f"y kept shrinking past the floor: {wy:.17g} -> {_width(deeper['y']):.17g}"


def test_issueA1_zooming_while_maximized_still_reveals_the_reset_button(probe):
    """⛶ Maximize moves .rich-output out of the card and into the overlay, so a
    reset button looked up from the canvas at callback time (`canvas.closest
    ('.rich-wrap')` -> null) is never found: zoom in the big view, restore, and
    the chart is left zoomed with the button hidden — the same dead end as A1.
    """
    mx = probe["maximize"]
    assert mx["built"], "the chart never rendered"
    assert mx["max_click"]["clicked"], mx["max_click"]
    assert mx["maximized"] == {"inOverlay": True, "inCard": False}, \
        f"the output element was not moved into the overlay: {mx['maximized']}"
    assert _width(mx["after_zoom"]["x"]) < _width(mx["before"]["x"]), \
        "the ctrl+wheel did not zoom inside the overlay; the case never ran"
    assert _width(mx["after_zoom"]["x"]) > 0, mx["after_zoom"]["x"]
    assert mx["restore_click"]["clicked"], mx["restore_click"]
    assert mx["restored"] == {"inOverlay": False, "inCard": True}, \
        f"the output element did not go back into the card: {mx['restored']}"
    btn = mx["btn_after_restore"]
    assert btn["h"] > 0, \
        f"restored from maximize still zoomed, reset button hidden: {btn}"
    assert mx["reset_click"]["clicked"], mx["reset_click"]
    assert mx["after_reset"]["x"] == mx["before"]["x"], mx["after_reset"]["x"]
    assert mx["after_reset"]["y"] == mx["before"]["y"], mx["after_reset"]["y"]


# ── A2: the empty data table ─────────────────────────────────────────────────
def test_issueA2_scatter_data_table_lists_every_point(probe):
    """Rows were built from `data.labels`, which a scatter does not have, so the
    `{x,y}` branch one line below was unreachable and the table came out with
    headers and `rows: []` — and the base instruction tells the model it need not
    repeat the numbers in prose, so they were gone entirely.
    """
    tbl = probe["scatter"]["table"]
    assert tbl, "no data table was rendered at all"
    assert tbl["rows"], f"the scatter data table is empty: {tbl}"
    assert len(tbl["rows"]) == 6, f"expected one row per point, got {tbl['rows']}"
    assert tbl["head"] == ["series", "x", "y"], tbl["head"]
    assert tbl["rows"][0] == ["obs", "1", "2"], tbl["rows"][0]
    assert tbl["rows"][3] == ["obs", "4", "16"], tbl["rows"][3]
    # the second dataset is present, not just the first
    assert tbl["rows"][4] == ["ref", "1", "1"], tbl["rows"][4]
    assert tbl["rows"][5] == ["ref", "4", "4"], tbl["rows"][5]


def test_issueA2_bubble_data_table_carries_the_radius(probe):
    """Same empty table for bubble, whose third dimension `r` is the only place
    the point size is written down.
    """
    tbl = probe["bubble"]["table"]
    assert tbl and tbl["rows"], f"the bubble data table is empty: {tbl}"
    assert tbl["head"] == ["series", "x", "y", "r"], tbl["head"]
    assert tbl["rows"] == [["cities", "13.4", "52.5", "12"],
                           ["cities", "2.35", "48.9", "9"]], tbl["rows"]


def test_issueA2_labelled_chart_data_table_still_reads_per_label(probe):
    """The label path is the one that worked; fixing scatter must not reroute a
    labelled line chart through the per-point path.
    """
    tbl = probe["line"]["table"]
    assert tbl, "no data table was rendered for the line chart"
    assert tbl["head"] == ["#", "v"], tbl["head"]
    assert tbl["rows"] == [["A", "3"], ["B", "1"], ["C", "4"],
                           ["D", "1"], ["E", "5"]], tbl["rows"]
