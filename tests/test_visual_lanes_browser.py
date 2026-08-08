# SPDX-License-Identifier: AGPL-3.0-or-later
"""B4 / B5 / B8 measured in a real browser (molecule clip, geometry dark-mode
contrast, abc reset duration).

The browser work lives in ``tests/visual_probe.py``; it runs once per session under
whichever interpreter has Playwright (the project venv does not), so the suite still
runs on a bare checkout — it skips instead. Same design as ``test_chart_browser.py``.

These assert on measured pixels and computed colour, never on source text:

  * **B4** ``scrollHeight <= clientHeight`` for the molecule card — nothing is cut off.
  * **B5** a WCAG contrast ratio computed from the label's ``getComputedStyle().fill``
    and the composited card background, ``>= 4.5:1`` in dark mode (and still ``>= 4.5``
    in a light render that follows a dark one, proving the global JXG.Options change
    does not leak).
  * **B8** the millisecond value the reset timer is scheduled from is ``> 0`` — it was
    ``0`` because ``getTotalTime()`` returned ``null``.

Each is paired with a tests/mutations.json entry shown to make the matching assertion
go red.
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
_PROBE = pathlib.Path(__file__).with_name("visual_probe.py")

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


def _playwright_python():
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
    py = _playwright_python()
    if py is None:
        pytest.skip("no interpreter with playwright installed "
                    "(set REDIRECALL_TEST_PLAYWRIGHT_PYTHON)")
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


# ── B4: molecule clip ────────────────────────────────────────────────────────
def test_issue_b4_molecule_card_does_not_clip(probe):
    """The molecule SVG lays out square (measured 530×530) but the card is a fixed
    320px tall, and `.rich-output svg{height:auto}` let it overflow: clientHeight 320
    vs scrollHeight 439, flex-centred so ~60px is sliced off *each* end (aspirin's
    COOH, caffeine here). Nothing may be cut off: scrollHeight <= clientHeight.
    """
    m = probe["molecule"]
    assert m.get("built"), f"the molecule never rendered: {m}"
    assert m["scrollH"] <= m["clientH"], \
        f"molecule clipped: scrollHeight {m['scrollH']} > clientHeight {m['clientH']} " \
        f"(svg {m['svgW']}x{m['svgH']})"
    # and it must still be a real, visibly-sized drawing, not shrunk to nothing
    assert m["svgH"] and m["svgH"] >= 120, f"molecule collapsed too small: {m}"


# ── B5: geometry dark-mode contrast ──────────────────────────────────────────
def test_issue_b5_geometry_labels_meet_wcag_in_dark_mode(probe):
    """The geometry lane had no dark-mode handling: axis/tick/point labels painted
    pure black (rgb(0,0,0)) on the ~rgb(63,63,71) dark card — a measured 1.98:1,
    below the 4.5:1 WCAG AA floor for text. The worst label's computed contrast must
    be >= 4.5:1.
    """
    g = probe["geometry_dark"]
    assert g.get("built"), f"the geometry board never rendered: {g}"
    assert g["nLabels"] >= 3, f"expected axis + element labels, got {g}"
    assert g["minContrast"] is not None and g["minContrast"] >= 4.5, \
        f"dark-mode label contrast {g['minContrast']}:1 < 4.5:1 " \
        f"(bg {g['bg']}, worst {min(g['labels'], key=lambda l: l['contrast'])})"


def test_issue_b5_light_mode_after_dark_does_not_leak(probe):
    """JXG.Options is a process-wide singleton; if the dark colours were set without
    restoring the defaults, the next LIGHT board would paint light labels on the
    light card. A light render that follows a dark one must still clear 4.5:1.
    """
    g = probe["geometry_light"]
    assert g.get("built"), f"the light geometry board never rendered: {g}"
    assert g["minContrast"] is not None and g["minContrast"] >= 4.5, \
        f"light-mode contrast {g['minContrast']}:1 < 4.5:1 — dark colours leaked: " \
        f"{min(g['labels'], key=lambda l: l['contrast'])}"


# ── B8: abc reset duration ───────────────────────────────────────────────────
def test_issue_b8_abc_reset_timer_gets_a_positive_duration(probe):
    """`visualObj.getTotalTime()` returns null for the synth-only play path, so the
    old `ms = round((getTotalTime()||0)*1000)` was 0 and the reset timer never fired
    — the button stayed "⏸ Stop" forever. The value must come from the primed synth's
    real length (measured synth.duration ≈ 2.9s) and be > 0.
    """
    a = probe["abc"]
    assert a.get("rendered"), f"the tune never rendered: {a}"
    assert a.get("supportsAudio"), "headless chromium reports no audio support"
    # the root cause, measured: the old source is null
    assert a["getTotalTime"] in (None, "noFn", 0), \
        f"getTotalTime unexpectedly returned {a['getTotalTime']!r}; the bug premise changed"
    # the synth knows the real length
    assert a["synthDuration"] and a["synthDuration"] > 0, \
        f"synth.duration is not positive: {a['synthDuration']}"
    # and the reset timer is scheduled from a positive ms
    assert a["resetMs"] > 0, \
        f"reset delay is {a['resetMs']}ms — the button would never reset (getTotalTime " \
        f"{a['getTotalTime']!r}, synth.duration {a['synthDuration']})"


def test_issue_b8_reset_delay_is_wired_to_synth_duration_not_gettotaltime():
    """Guards the wiring, not only the helper (the probe above calls _abcDurationMs
    directly, so a revert of just the _abcPlay line would slip past it): the reset
    delay in _abcPlay must be computed by _abcDurationMs(out._abcSynth) and must not
    fall back to the tune's getTotalTime(), which returns null. Source-level, and
    paired with a mutations.json entry that reverts this exact line and has been shown
    to KILL this test.
    """
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("// No 'ended' event on the basic synth")
    window = html[i:html.index("ms+250", i)]
    assert "const ms=_abcDurationMs(out._abcSynth);" in window, window
    assert "getTotalTime" not in window, f"reset delay still references getTotalTime: {window!r}"
