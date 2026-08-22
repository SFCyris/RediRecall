# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accessibility and destructive-action safety, measured in a real browser.

The browser work lives in ``tests/a11y_probe.py``; it runs once per session under
whichever interpreter has Playwright (the project venv does not), so the suite still
runs on a bare checkout — it skips instead. Same design as ``test_visual_lanes_browser.py``.

Every assertion here is on a COMPUTED value, never on source text. That is the point:
the defects being guarded were all invisible to a grep — a focus ring present in the CSS
but measuring 1.15:1, a dialog with `role` but no keyboard exit, an Escape handler that
closed the wrong layer, a confirm() that fired but named nothing.
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
_PROBE = pathlib.Path(__file__).with_name("a11y_probe.py")

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
def a11y():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    # SKIP rather than fail on a harness problem. A bare assert here turns one flaky
    # browser launch into ~30 simultaneous failures across unrelated behaviours, which
    # also makes the mutation sweep read those as kills when nothing was tested at all.
    if not out:
        pytest.skip(f"a11y probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"a11y probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"a11y probe could not run: {data.get('error')}")
    return data


def test_a_missing_browser_skips_by_default_and_fails_when_ci_demands_one(monkeypatch):
    """On CI a silent skip is indistinguishable from a pass — these tests would be a
    no-op with nothing in the output saying so. REDIRECALL_REQUIRE_BROWSER_TESTS=1 turns
    a missing interpreter into a failure, so a pipeline can assert the suite really ran."""
    import _pytest.outcomes as outcomes
    import test_a11y_browser as mod

    monkeypatch.delenv("REDIRECALL_REQUIRE_BROWSER_TESTS", raising=False)
    with pytest.raises(outcomes.Skipped) as skipped:
        mod._no_browser()
    assert "playwright" in str(skipped.value).lower()

    monkeypatch.setenv("REDIRECALL_REQUIRE_BROWSER_TESTS", "1")
    with pytest.raises(outcomes.Failed) as failed:
        mod._no_browser()
    assert "REDIRECALL_REQUIRE_BROWSER_TESTS=1" in str(failed.value)


# ── contrast (WCAG 1.4.3) ────────────────────────────────────────────────────
@pytest.mark.parametrize("token", ["--green", "--yellow", "--red", "--blue",
                                   "--accent", "--text2"])
def test_light_theme_semantic_colours_meet_the_body_text_floor(a11y, token):
    """The default theme is light, and these tokens are used far more as text than as
    fill (red 51 text usages vs 6 fill). They measured 3.19–3.76:1 on the near-white
    card — below the 4.5:1 floor — which put the RAG "no match" badge, the cost pill and
    every Delete button's label under it."""
    got = a11y["contrast"][token]
    assert got >= 4.5, f"{token} measures {got}:1 on the light card, needs >= 4.5:1"


def test_referenced_colour_tokens_are_actually_defined(a11y):
    """--text3 drives the "not configured" provider dot and --muted the model hint. Both
    were used but never declared, so the dots rendered as nothing at all."""
    for name, val in a11y["tokens_defined"].items():
        assert val, f"{name} is referenced in the CSS but resolves to an empty value"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_white_text_on_every_accent_filled_control_is_readable(a11y, theme):
    """Measured per CONTROL, not per token. The first version of this test read
    --accent-fill/--accent-fill2 and passed green while five real controls still painted
    white text on --accent at 2.59:1 — it could not fail for the defect it named."""
    rows = a11y["fill_contrast"][theme]
    failing = {k: v for k, v in rows.items() if v is not None and v < 4.5}
    assert not failing, f"{theme}: white text under 4.5:1 on {failing}"


@pytest.mark.parametrize("control", ["btn-primary", "new-chat-btn", "send-btn",
                                     "seg-btn-active", "prov-use-active",
                                     "mp-btn", "mp-item-selected",
                                     "msg-bubble-user", "msg-avatar-user"])
def test_every_accent_filled_control_is_actually_measured(a11y, control):
    """A `None` here means the control painted no accent fill, so the contrast check
    silently skipped it — which is how five of these went unnoticed. Fail loudly instead
    of scoring a pass on a control nobody measured."""
    for theme in ("light", "dark"):
        assert a11y["fill_contrast"][theme].get(control) is not None, \
            f"{control} has no accent fill in {theme} — the check is not covering it"


# ── focus (WCAG 2.4.7, 2.4.3) ────────────────────────────────────────────────
def test_focus_outline_is_measured_on_more_than_one_control(a11y):
    """The previous probe measured exactly one element — and not the one it named, because
    its target sat in a closed dialog where .focus() is a no-op."""
    rings = a11y["focus_rings"]
    assert len(rings) >= 3, f"only {len(rings)} control(s) measured: {rings}"
    assert len({r["tag"] for r in rings}) >= 3, f"all of one element type: {rings}"


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_every_measured_control_has_a_real_focus_outline(a11y, idx):
    """These controls set outline:none, so a 12%-alpha box-shadow was the ONLY focus
    indicator — measured 1.15:1, i.e. invisible. A higher-specificity `outline:none` on
    any one of them re-opens the hole, which is why each is asserted separately.

    Measured the instant focus lands. The outline is deliberately excluded from these
    controls' `transition: all`, so a ring that only fades in over 250ms fails here — a
    focus indicator has to be present immediately, not a quarter of a second later.
    """
    rings = a11y["focus_rings"]
    if idx >= len(rings):
        pytest.skip(f"only {len(rings)} controls measured")
    r = rings[idx]
    if not r["focused"]:
        pytest.skip(f"{r['sel']} did not take focus in this run (browser contention)")
    assert r["outlineStyle"] not in ("none", ""), \
        f"{r['sel']} ({r['tag']}) has no focus outline (rule matched: {r['ruleMatches']})"
    width = float(r["outlineWidth"].replace("px", "") or 0)
    assert width >= 2, (
        f"{r['sel']} ({r['tag']}) shows a {width}px focus ring, needs >= 2px "
        f"(rule matched: {r['ruleMatches']})")


def test_at_least_one_control_has_a_measured_focus_outline(a11y):
    """Keeps the rule-matching fallback above honest: if EVERY control fell back to
    "a rule matches", the suite would be asserting CSS text again rather than pixels.

    Skips rather than fails when the browser never delivered focus at all. Under parallel
    mutation labs the keyboard dance can land nowhere, and a hard failure here was being
    attributed to whatever mutation happened to be running — which makes the gate report
    kills it did not earn.
    """
    rings = a11y["focus_rings"]
    if not any(r["focused"] for r in rings):
        pytest.skip(f"no control took focus in this run (browser contention): {rings}")
    measured = [r for r in rings
                if float(r["outlineWidth"].replace("px", "") or 0) >= 2]
    assert len(measured) == len([r for r in rings if r["focused"]]), (
        f"some focused controls showed no ring: "
        f"{[r for r in rings if r['focused'] and r not in measured]}")


def test_focus_moves_into_a_dialog_and_returns_to_its_trigger(a11y):
    """Focus was never managed: opening Settings left focus on the page behind it, and
    closing dropped focus to <body> so a keyboard user restarted from the top."""
    lay = a11y["layering"]
    assert lay["focusInPanel"], "opening Settings does not move focus into the panel"
    assert lay["focusInModal"], "opening a dialog does not move focus into it"
    assert lay["focusReturned"] == lay["start"], (
        f"focus went to {lay['focusReturned']!r}, not back to the trigger {lay['start']!r}")


def test_escape_closes_only_the_innermost_layer(a11y):
    """Escape called closeSettings() and closeSearch() unconditionally, so pressing it
    over a confirmation dialog (z-index 300) shut the Settings panel underneath it
    (z-index 100) and left the dialog floating over the chat — and the dialog itself had
    no keyboard exit at all."""
    lay = a11y["layering"]
    assert lay["afterFirst"]["modal"] is False, "Escape did not close the dialog"
    assert lay["afterFirst"]["settings"] is True, \
        "Escape closed the Settings panel underneath the open dialog"
    assert lay["settingsClosed"], "a second Escape did not close Settings"


def test_a_docked_panel_does_not_capture_tab(a11y):
    """The pinned panel is a docked side panel (aria-modal false, z-index 50) that leaves
    the rest of the page usable. Confining Tab to it made the composer, sidebar and topbar
    unreachable for as long as it was open — a WCAG 2.1.2 keyboard trap created by the
    change meant to improve keyboard access."""
    assert a11y["layering"]["pinnedTrapsTab"] is False, \
        "Tab is trapped inside the non-modal pinned panel"


def test_escape_follows_the_painted_stacking_order(a11y):
    """Escape order must match z-index (modal 300 > search 150 > settings 100 > pinned 50),
    not source order. Listed pinned-first, one Escape closed the panel hidden *behind* the
    full-screen Settings overlay."""
    got = a11y["layering"]["escWithPinnedBehindSettings"]
    assert got["settings"] is False, "Escape did not close the topmost layer (Settings)"
    assert got["pinned"] is True, "Escape closed the panel underneath the Settings overlay"


def test_escape_over_a_fullscreen_overlay_closes_only_the_overlay(a11y):
    """The lightbox and the maximize overlay sit at z-index 9999 and install their own
    Escape handler. Both handlers are on `document`, so without an explicit stand-down one
    press closed the overlay AND the layer underneath it."""
    assert a11y["layering"]["lightboxOpened"], "the lightbox did not open — nothing measured"
    got = a11y["layering"]["escOverLightbox"]
    assert got["lightbox"] is False, "Escape did not close the lightbox"
    assert got["settings"] is True, "Escape also closed the Settings panel underneath it"


def test_escape_inside_the_search_box_closes_exactly_one_layer(a11y):
    """An inline onkeydown closed search in the target phase; the event then bubbled to the
    layered handler, which no longer saw search open and closed the layer beneath it."""
    got = a11y["layering"]["escFromSearchInput"]
    assert got["search"] is False, "Escape in the search box did not close search"
    assert got["settings"] is True, "one Escape closed both search AND Settings"


def test_a_dialog_does_not_autofocus_its_destructive_action(a11y):
    """Focusing first-in-DOM-order landed on "Export & Delete", "Clear Chunks" and
    "Deduplicate": opening the dialog and pressing Enter destroyed the instance the dialog
    existed to protect."""
    label = a11y["layering"]["autoFocusedLabel"].lower()
    assert "delete" not in label and "clear" not in label, \
        f"the dialog auto-focused a destructive action: {a11y['layering']['autoFocusedLabel']!r}"


# ── names and structure (WCAG 1.3.1, 4.1.2, 4.1.3) ───────────────────────────
def test_status_messages_have_a_live_region(a11y):
    """grep -c aria-live was 0 file-wide: a screen-reader user was never told an
    operation had failed, and the error toast self-destructs after 3 seconds."""
    assert a11y["aria"]["liveRegions"] >= 2


def test_dialogs_declare_themselves_as_dialogs(a11y):
    assert a11y["aria"]["dialogs"] >= 4, "overlays are missing role/aria-modal"


def test_document_has_a_heading_and_a_main_landmark(a11y):
    assert a11y["aria"]["h1"], "no <h1> — the first heading was <h2>Settings</h2>, in a hidden dialog"
    assert a11y["aria"]["main"], "no main landmark"


def test_no_form_control_is_left_without_an_accessible_name(a11y):
    """69 of 70 labels were visually adjacent but programmatically unlinked.

    Asserts the PROPERTY, not a count. A `labelsFor >= N` floor stops guarding as soon as
    the real number drifts above N — with 49 links and a floor of 41, eight labels could
    be removed and the test would still pass.
    """
    unnamed = a11y["aria"]["unnamedControls"]
    assert unnamed == [], (
        f"{len(unnamed)} form control(s) have no label, aria-label or aria-labelledby: "
        f"{unnamed}")


def test_every_toggle_has_an_accessible_name(a11y):
    """The <label> wrapped only the checkbox while the visible text sat in a sibling
    <span>, so every toggle in the app was announced as an unnamed checkbox."""
    aria = a11y["aria"]
    assert aria["totalToggles"] > 0
    assert aria["namedToggles"] == aria["totalToggles"], \
        f"{aria['totalToggles'] - aria['namedToggles']} toggle(s) still have no name"


def test_icon_only_buttons_have_accessible_names(a11y):
    """Enumerated, not a frozen list of two ids — and a missing element used to score a
    pass, so a rename or a brand-new unnamed button was invisible."""
    unnamed = a11y["aria"]["unnamedIconBtns"]
    assert unnamed == [], f"{len(unnamed)} icon-only button(s) with no accessible name: {unnamed}"


# ── destructive actions (Nielsen #5 — error prevention) ──────────────────────
@pytest.mark.parametrize("action", ["clearChat", "webSource", "template",
                                    "ragStats", "endpoint", "resetConfig"])
def test_destructive_actions_confirm_and_name_what_is_lost(a11y, action):
    """Five of these shipped with NO confirmation at all — including a one-click wipe of
    the open conversation, and deleting a Redis endpoint that strands every RAG instance
    on it. A dialog that does not say what is lost is barely better, so each one is
    checked for the specific detail too."""
    assert a11y["confirms"][action], \
        f"{action} does not raise a confirmation naming what is lost"


# ── motion (WCAG 2.2.2, Level A) ─────────────────────────────────────────────
def test_reduced_motion_stops_the_indefinite_animations(a11y):
    """Several animations run `infinite` for the whole length of an operation — the crawl
    bar for a multi-minute crawl, the streaming border for every answer — with no pause
    control anywhere, which is a Level A failure. Measured on a real element, because the
    media block has to actually win over the per-element `animation:` shorthand."""
    rm = a11y["reduced_motion"]
    assert rm["iterations"] == "1", f"animation still loops ({rm['iterations']}x) under reduce"
    dur = float(rm["duration"].rstrip("s") or 0)
    assert dur < 0.05, f"animation duration is {rm['duration']} under reduce"


def test_reduced_motion_also_stops_the_smil_avatar(a11y):
    """The streaming avatar uses SVG SMIL, which prefers-reduced-motion cannot reach — a
    CSS media query has no effect on <animate>. The preference has to be read in JS."""
    assert a11y["reduced_motion"]["smilLoops"] == 0, \
        "the SMIL avatar still loops indefinitely under reduce"
    assert a11y["motion_default"] > 0, \
        "the avatar no longer animates at all — the preference check inverted the default"


def test_the_page_logs_no_errors_on_load(a11y):
    assert not a11y["console"], f"the page logged errors: {a11y['console']}"
