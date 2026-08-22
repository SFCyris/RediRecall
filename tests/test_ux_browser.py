# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workflow fixes, measured in a real browser.

The browser work lives in ``tests/ux_probe.py``; it runs once per session under whichever
interpreter has Playwright, so the suite still runs on a bare checkout — it skips instead.
Same design as ``test_a11y_browser.py``.

Every assertion is on an observed value: the pixel width the progress bar actually paints,
the JSON body a Cancel button actually posted, whether a toast is still in the DOM four
seconds later. None of these defects were visible to a grep — the old progress bar carried
a literal ``50%``, and Pause read a text field that had nothing to do with the crawl.
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
_PROBE = pathlib.Path(__file__).with_name("ux_probe.py")

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
    """Skip — or FAIL when CI insists the browser tests must really run."""
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
def ux():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    # SKIP rather than fail on a harness problem: one flaky browser launch should not
    # read as two dozen unrelated behavioural failures.
    if not out:
        pytest.skip(f"ux probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"ux probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"ux probe could not run: {data.get('error')}")
    return data


def test_a_missing_browser_skips_by_default_and_fails_when_ci_demands_one(monkeypatch):
    """A silent skip is indistinguishable from a pass in CI output."""
    import _pytest.outcomes as outcomes
    import test_ux_browser as mod

    monkeypatch.delenv("REDIRECALL_REQUIRE_BROWSER_TESTS", raising=False)
    with pytest.raises(outcomes.Skipped):
        mod._no_browser()
    monkeypatch.setenv("REDIRECALL_REQUIRE_BROWSER_TESTS", "1")
    with pytest.raises(outcomes.Failed):
        mod._no_browser()


# ── 1: crawl progress means something at the shipped default ─────────────────

def test_crawl_progress_tracks_pages_done_against_pages_discovered(ux):
    """max_pages defaults to 0 (unlimited), and the old bar only moved when it was > 0 —
    so the default crawl showed a 2% pulse from start to finish. A BFS has no total until
    it ends, but done-against-discovered is a real ratio and it is the one shown.

    Measured in painted pixels, not in the style string: the transition is disabled in the
    probe first, because reading the rect mid-animation returned the same value for every
    case and would have hidden a bar that never moved.
    """
    cp = ux["crawl_progress"]
    early, late = cp["unlimited_early"], cp["unlimited_late"]
    assert round(early["frac"], 2) == 0.05, early     # 3 of 60
    assert round(late["frac"], 2) == 0.75, late       # 45 of 60
    assert late["px"] > early["px"] * 10, (early, late)


def test_crawl_progress_states_both_numbers_and_which_total_it_is_using(ux):
    """A bar with no caption cannot say whether 25% means "of the page limit" or "of what
    has been found so far", and the second denominator grows as the crawl runs."""
    cp = ux["crawl_progress"]
    assert "3 of 60 pages found so far" in cp["unlimited_early"]["note"]
    assert "57 queued" in cp["unlimited_early"]["note"]
    assert "grows" in cp["unlimited_early"]["note"]
    assert cp["capped"]["note"] == "25 of 100 pages (page limit)"
    assert round(cp["capped"]["frac"], 2) == 0.25


def test_a_crawl_with_nothing_discovered_yet_stays_indeterminate(ux):
    """Before the first page comes back there is no ratio to show, and inventing one is
    what the hard-coded 50% did."""
    n = ux["crawl_progress"]["nothing_yet"]
    assert n["frac"] < 0.05, n
    assert n["note"] == "Starting…"


def test_a_paused_crawl_says_so_in_the_progress_caption(ux):
    assert ux["crawl_progress"]["paused"]["note"].endswith("· paused")


# ── 2: reattaching to a running crawl ────────────────────────────────────────

def test_reattaching_does_not_overwrite_the_url_the_user_is_typing(ux):
    """#crawl-url is the NEXT crawl the user is composing. Reattach used to write the
    running crawl's URL into it, discarding a half-typed address."""
    assert ux["crawl_target"]["urlBoxAfterAttach"] == "https://typed-by-the-user.example/new"


def test_reattaching_shows_the_crawls_measured_progress_not_a_placeholder(ux):
    """Re-attaching painted a literal 50% — a number with no relationship to the crawl,
    on a bar the user reads as "roughly half done". Measured in painted pixels against
    4 pages done of 30 discovered.
    """
    frac = ux["crawl_target"]["barFrac"]
    assert abs(frac - 4 / 30) < 0.02, frac
    assert abs(frac - 0.5) > 0.1, "the bar is still showing the hard-coded 50%"


def test_reattaching_names_the_crawl_it_attached_to(ux):
    assert "a.example/docs" in ux["crawl_target"]["attachedShown"]


def test_other_running_crawls_are_reachable_not_silently_dropped(ux):
    """It bound to running[0] and offered no route to any other crawl, so a second one was
    invisible and uncancellable from the UI."""
    assert ux["crawl_target"]["otherCrawlOffered"], ux["crawl_target"]
    assert "1 other crawl" in ux["crawl_target"]["attachedShown"]


def test_reattaching_to_a_paused_crawl_shows_it_as_paused(ux):
    """The server owns the pause flag; reattach used to reset it to false locally, so the
    button offered to Pause a crawl that was already paused."""
    assert ux["reattach_paused"]["btn"].strip() == "▶ Resume"
    assert ux["reattach_paused"]["note"].endswith("· paused")


# ── 3: pause and cancel address the right crawl ──────────────────────────────

@pytest.mark.parametrize("field", ["pauseBody", "cancelBody"])
def test_pause_and_cancel_target_the_attached_crawl_not_the_url_box(ux, field):
    """Both read #crawl-url. Type a different URL mid-crawl — which the input invites,
    since it is also where the next crawl is composed — and Pause/Cancel addressed a URL
    with no crawl behind it while the real one ran on.
    """
    body = json.loads(ux["crawl_target"][field])
    assert body["url"] == "https://a.example/docs", body
    assert "typed-by-the-user" not in body["url"]


def test_cancelling_releases_the_attachment(ux):
    assert ux["crawl_target"]["clearedAfterCancel"]


# ── 4: file ingestion can be seen and stopped ────────────────────────────────

def test_a_running_ingest_is_found_on_reopening_and_can_be_cancelled(ux):
    """A file ingest had no id, no /api/ingest/active and no cancel: losing the stream left
    it indexing with nobody able to find it, let alone stop it."""
    ic = ux["ingest_cancel"]
    assert ic["visible"], "the cancel button did not appear for a running ingest"
    assert json.loads(ic["body"]) == {"job": "job-abc123"}
    assert "notes.pdf" in ic["status"] and "3/5" in ic["status"]


# ── 5: settings no longer discard staged edits silently ──────────────────────

def test_closing_settings_with_staged_edits_asks_before_discarding(ux):
    """Escape, the backdrop and Cancel all threw away unsaved API keys and tuning without
    a word, while instance and endpoint edits in the same panel had already been applied."""
    d = ux["settings_dirty"]
    assert d["dirtyBadge"], "no unsaved-changes indicator after editing a field"
    assert d["stillOpen"], "the panel closed and took the edit with it"
    assert d["askedFirst"], "no confirmation was shown"
    assert d["prompt"] == "Discard unsaved settings?"


def test_closing_an_untouched_settings_panel_does_not_nag(ux):
    """A confirm on every close would train people to dismiss it unread."""
    d = ux["settings_dirty"]
    assert d["cleanBadge"], "the unsaved badge showed on an untouched panel"
    assert d["closedWhenClean"], "an untouched panel refused to close"


# ── 6: the first run leads somewhere ─────────────────────────────────────────

def test_with_no_model_the_welcome_screen_offers_setup_instead_of_chips(ux):
    """The chips led to a three-second "Select a model first" toast and no route to the
    thing that fixes it."""
    u = ux["first_run"]["unconfigured"]
    assert u["setupShown"], "no setup card on a welcome screen with no model"
    assert not u["chipsShown"], "chips that cannot work were still offered"
    assert u["routesToSettings"]


def test_with_a_model_the_welcome_screen_is_unchanged(ux):
    c = ux["first_run"]["configured"]
    assert c["chipsShown"] and not c["setupShown"], c


# ── 7: the Status tab covers every provider ──────────────────────────────────

def test_the_status_tab_reports_all_seven_providers(ux):
    """It probed four. A Gemini or Groq user opened System Status and found nothing about
    the provider they were running on."""
    assert ux["status_tab"]["named"] == [
        "Ollama", "Claude", "OpenAI", "Qwen", "Mistral", "Groq", "Gemini"]


def test_an_unconfigured_provider_is_offered_a_way_to_configure_it(ux):
    assert ux["status_tab"]["offersSetUp"]


def test_the_button_naming_removed_ui_is_gone(ux):
    """"⟳ Update dots" called a function that does not touch any dot."""
    assert not ux["status_tab"]["deadButton"]


def test_the_provider_dots_are_actually_painted(ux):
    """Seven dots existed in the markup and no code ever set their colour, so all seven
    stayed at the "not configured" grey whatever was configured. Asserted as three
    DIFFERENT measured colours — reachable, failing and unconfigured — because a single
    colour check passes just as well when every dot is stuck on one value.
    """
    dots = ux["status_tab"]["dots"]
    assert len({dots["gemini"], dots["openai"], dots["qwen"]}) == 3, dots
    assert dots["gemini"] == "rgb(21, 128, 61)", dots      # --green, reachable
    assert dots["openai"] == "rgb(220, 38, 38)", dots      # --red, configured but failing
    assert dots["qwen"] == "rgb(118, 118, 121)", dots      # --text3, never configured


# ── 8: scheduled re-crawl has a UI ───────────────────────────────────────────

def test_the_recrawl_schedule_is_listed_and_editable(ux):
    """Four endpoints and a background scheduler shipped with no UI at all, so a feature
    the README advertises could only be reached by hand-editing the config file."""
    r = ux["recrawl"]
    assert r["hasRow"] and r["hasRemove"], r["rendered"]
    assert "Last re-crawled" in r["rendered"]


def test_scheduling_a_url_posts_the_instance_and_depth_beside_it(ux):
    body = json.loads(ux["recrawl"]["addBody"])
    assert body["url"] == "https://new.example/docs"
    # Never blank: the select is empty until instances load, and an instance named ""
    # is one no crawl will ever write to.
    assert body["instance"], body
    assert "depth" in body


def test_recrawl_can_be_triggered_now_and_its_toggle_is_actually_saved(ux):
    """POST /api/config merges at the top level, so a `recrawl` key left out of the payload
    means the on/off switch and the interval are inert controls."""
    assert ux["recrawl"]["triggered"]
    assert ux["recrawl"]["toggleExists"]
    assert ux["recrawl"]["inPayload"], "recrawl is missing from the saved settings payload"


# ── 9: errors stop disappearing ──────────────────────────────────────────────

def test_an_error_toast_outlives_the_old_three_second_expiry(ux):
    """Every toast was removed after 3000ms regardless of severity, so the message that
    most needed reading was the one that vanished fastest — with no log to recover it."""
    t = ux["toasts"]
    assert any("disk on fire" in a for a in t["alive"]), t["alive"]
    assert not any("all good" in a for a in t["alive"]), \
        "a success toast is still up after 4s; the auto-dismiss stopped working"


def test_a_persistent_toast_can_be_dismissed(ux):
    """A message that never expires needs a way out or it covers the app for good."""
    assert ux["toasts"]["closable"]
    assert ux["toasts"]["afterDismiss"] == 0


def test_every_message_is_recoverable_from_the_notification_log(ux):
    assert ux["toasts"]["loggedError"] and ux["toasts"]["loggedSuccess"]


def test_a_repeated_error_collapses_into_one_counted_toast(ux):
    """Errors no longer expire on their own, and one failure often arrives many times over:
    searching every conversation with Redis down raises one per conversation. Without
    collapsing them the screen fills with identical toasts that each need dismissing —
    a pile created by combining the persistent-error fix with the all-conversations search.
    """
    d = ux["toast_dedup"]
    assert d["count"] == 2, d          # twelve identical + one distinct
    assert d["badge"] == "×12", d
    assert any("a different problem" in x for x in d["texts"]), d


# ── 10: the browser keeps its own Find ───────────────────────────────────────

def test_plain_cmd_f_is_left_to_the_browser(ux):
    """It was intercepted and replaced with a search over stored message text only — a
    strictly weaker tool than the native find, taking the shortcut people expect."""
    f = ux["find_key"]
    assert not f["plainPrevented"], "Ctrl/Cmd+F was preventDefault()ed"
    assert not f["openedOnPlain"]


def test_shift_cmd_f_opens_the_app_search(ux):
    f = ux["find_key"]
    assert f["shiftPrevented"] and f["openedOnShift"]


def test_search_reports_how_many_matches_it_found(ux):
    """It listed results with no count, so "did that find anything?" needed counting rows."""
    assert ux["search"]["oneSession"]["count"] == "2 matches"


def test_search_reaches_retrieved_source_text_and_other_conversations(ux):
    """It searched the current conversation's message bodies and nothing else — not the
    retrieved chunks, not any other conversation."""
    s = ux["search"]
    assert s["allSessions"]["hits"] == 3, s
    assert "2 conversation" in s["allSessions"]["count"], s
    assert s["allSessions"]["mentionsOther"]
    # Turning the chunk scope off has to actually drop the chunk hit, otherwise the
    # control is decorative and the count above proves nothing.
    assert s["noChunks"] == 2, s


def test_the_suggestion_chips_are_actually_hidden_when_no_model_is_available(ux):
    """`hidden` on a `display:flex` element does nothing: an author-origin display beats
    the user agent's `[hidden]{display:none}` whatever the specificity. So the setup card
    appeared and all four chips stayed on screen beside it — each still one click into the
    dead end the card exists to replace. Measured as a computed style and a painted box,
    not as the attribute, because the attribute was already right.
    """
    c = ux["chips_hidden"]
    assert c["attr"] is True, c
    assert c["display"] == "none", c
    assert not c["painted"], "the chips are still on screen with no model configured"
    assert c["restored"], "the chips did not come back once a model was available"


def test_cancelling_an_adopted_crawl_does_not_kill_this_tabs_own_stream(ux):
    """The attachment chooser means the panel can point at a crawl started elsewhere, while
    this tab is streaming a different one. Cancel aborted the local stream unconditionally:
    the server cancelled B, the client tore down A, and A kept crawling with the panel
    cleared — the same wrong-target bug the Pause/Cancel fix was raised for.
    """
    c = ux["cancel_stream"]
    assert c["cancelledOnServer"] == "https://b.example/", c
    assert not c["abortedLocalStream"], "cancelling crawl B aborted crawl A's stream"
    assert c["abortedOwnStream"], "cancelling this tab's own crawl left its stream running"


def test_the_crawl_poll_stops_instead_of_running_for_the_life_of_the_page(ux):
    """The only stop inside the poll body sat behind `if(!match)return` — so every state
    where the attached crawl stops appearing in the listing (finished and reaped, or the
    attachment released by a local crawl ending first) left it polling once a second
    forever. Closing the panel did not stop it either.
    """
    p = ux["poll_stops"]
    assert p["started"], "the poll never started, so this proves nothing"
    assert p["stoppedWhenGone"], "the poll kept running after its crawl disappeared"
    assert p["runningAgain"], "the poll did not restart on re-attach"
    assert p["stoppedOnClose"], "closing Settings left the poll running"


def test_an_adopted_ingest_keeps_up_with_the_job_and_reports_how_it_ended(ux):
    """It painted one snapshot and never refreshed: "Indexing report.pdf (3/10)" stayed on
    screen with a live Cancel button long after the job had finished, and clicking it then
    said the ingest had already finished. It also bound to live[0] with no way to reach a
    second job — the exact behaviour the crawl chooser was added to fix.
    """
    i = ux["ingest_poll"]
    assert "one.pdf" in i["first"] and "1/4" in i["first"], i
    assert i["chooser"], "a second running ingest was unreachable"
    assert "three.pdf" in i["advanced"] and "3/4" in i["advanced"], i
    assert "3 file(s)" in i["finished"] and "1 error" in i["finished"], i
    assert i["btnGone"], "Cancel stayed live on a finished job"


def test_a_filename_with_an_ampersand_is_not_double_encoded(ux):
    """escHtml into textContent escapes twice: textContent already treats its input as
    literal, so "Q&A notes.pdf" rendered as "Q&amp;A notes.pdf"."""
    assert ux["ingest_name"]["text"].startswith("Indexing Q&A notes.pdf"), ux["ingest_name"]


def test_typing_does_not_refetch_every_conversation_once_per_character(ux):
    """`_stub` is cleared only after the fetch resolves, so keystroke n+1 saw every
    conversation as still unloaded: typing six characters against 20 stored conversations
    issued up to 120 concurrent requests, and on a failing server one error toast each.
    """
    f = ux["search_fetches"]
    assert 0 < f["debounced"] <= f["stubs"], \
        f'{f["debounced"]} fetches for {f["stubs"]} conversations — one per keystroke'
    # Two searches that both fire, with the first still fetching, must share its work:
    # the debounce alone does not cover a slow server or a slow typist.
    assert f["overlapping"] <= f["stubs"], \
        f'{f["overlapping"]} fetches for {f["stubs"]} conversations across two searches'
    assert "match" in f["count"], f


# ── reachability: a panel only loads if the route people take loads it ───────

def test_every_settings_tab_loads_when_its_button_is_clicked(ux):
    """Per-tab loading used to hang off individual onclick attributes and off openSettings,
    so whichever route was not updated loaded nothing. Three panels shipped that way and
    all three were reachable only by a deep link nothing in the UI actually followed: the
    re-crawl table rendered blank from the tab button, the provider dots stayed at the
    "not configured" grey, and a running ingest was never looked for at all.

    Driven by clicking the real tab buttons, not by calling the loaders.
    """
    e = ux["tab_entry"]
    assert "sched.example" in e["recrawl"], e["recrawl"]
    assert e["dot"] == "rgb(21, 128, 61)", e          # --green: probed and reachable
    assert e["ingestVisible"], e


# ── an edit the input/change watch cannot see ────────────────────────────────

def test_switching_provider_counts_as_an_unsaved_change(ux):
    """setProvider writes straight into the payload Save Settings posts, but a button click
    fires neither input nor change — so the panel closed silently and discarded it, while
    its own discard dialog described exactly that loss."""
    d = ux["provider_dirty"]
    assert d["before"] == "ollama" and d["after"] == "openai", d
    assert d["dirty"] and d["badgeShown"], d
    assert d["askedFirst"], "Settings closed without warning after a provider switch"


# ── where Enter lands when Settings opens ────────────────────────────────────

def test_opening_settings_does_not_park_enter_on_export_config(ux):
    """The safe-button heuristic ("first .btn-secondary") means Cancel in a confirm dialog
    and "⬇️ Export Config" in the Settings panel — where pressing Enter downloads a JSON
    file containing every stored API key in plaintext."""
    f = ux["settings_focus"]
    assert f["inPanel"], f
    assert "Export" not in f["label"], f
    assert f["label"] == "Close settings", f


# ── the visible toast stack is bounded ───────────────────────────────────────

def test_a_run_of_distinct_errors_does_not_grow_without_bound(ux):
    """Errors no longer expire, so a repeating failure — a dropped WebSocket retrying every
    two seconds — grew the fixed, bottom-anchored column past the top of the viewport,
    where a fixed container cannot be scrolled and every toast still needs its own click.
    De-duplication only collapses repeats of the SAME message."""
    c = ux["toast_cap"]
    assert c["visible"] == 5, c
    assert c["keepsNewest"], "the cap dropped the newest message instead of the oldest"
    assert c["logged"] == 20, "the notification log must still hold every one of them"


# ── the client and the server must agree on what a crawl is called ───────────

def test_a_seed_url_with_a_fragment_still_matches_its_own_crawl(ux):
    """The crawler strips the fragment before keying anything on the URL. Tracking the raw
    form made every client-side comparison fail: the panel listed its OWN crawl under
    "1 other crawl running", and its poll never matched — so the bar froze at its first
    paint and the interval polled the server once a second for the life of the page."""
    f = ux["crawl_fragment"]
    assert f["stream"] == "https://frag.example/docs", f
    assert f["tracked"] == "https://frag.example/docs", f
    assert not f["listedAsOther"], "the panel offered to attach to the crawl it is on"
    assert f["rateShown"], "this tab was not recognised as owning its own crawl"
    assert "20 pages found" in f["note"], f


# ── the progress ratio's numerator ───────────────────────────────────────────

@pytest.mark.parametrize("case", ["incremental", "legacy"])
def test_progress_counts_pages_resolved_not_pages_indexed(ux, case):
    """A URL is DISCOVERED when it is admitted to the frontier — before the crawlable-type,
    already-indexed, robots, empty-text and duplicate checks, any of which end it without an
    index. Dividing pages_indexed by that number stalls: an incremental re-crawl skips
    almost everything, so 2 indexed of 40 resolved out of 50 found showed a 4% bar that
    crept nowhere and then snapped to 100%.

    Measured in painted pixels. The `legacy` case is a payload from before the server sent
    `resolved`, summed from the four counters client-side.
    """
    r = ux["crawl_resolved"][case]
    assert abs(r["frac"] - 0.8) < 0.02, r        # 40 resolved of 50 discovered
    assert "40 of 50 pages found so far" in r["note"], r
    assert "2 indexed" in r["note"], "the indexed count must still be visible"


def test_the_timeline_lane_repairs_colons_before_it_renders(ux):
    """The repair only counts if the lane calls it. Asserted on what the lane hands to
    mermaid, with and without the header the lane adds itself — the header path is where
    the reported failure came from, and its error pointed at `timeline` rather than at the
    colon, which is what made it look like a header bug.
    """
    t = ux["timeline_lane"]
    assert t["withoutHeader"] == "timeline\n2024-01-01 00#58;00 : Sunrise in New York", t
    assert t["withHeader"] == "timeline\n09#58;30 : Standup", t


# ── 11: .btn-ghost ───────────────────────────────────────────────────────────

def test_btn_ghost_is_a_real_rule_not_an_inherited_default(ux):
    """It was referenced by a modal Cancel button and never defined anywhere, so that one
    button rendered with no background, no border and whatever colour it inherited."""
    g = ux["btn_ghost"]
    assert g["ruleExists"], "no .btn-ghost rule in any stylesheet"
    assert not g["inheritedHostColour"], \
        "the button still takes its parent's colour — nothing is styling it"
    assert g["borderStyle"] == "solid" and g["borderWidth"] == "1px", g


# ── 12: citations line up with the inspector ─────────────────────────────────

def test_the_inspector_labels_a_chunk_with_the_number_the_answer_cites(ux):
    """#n was the chunk's position after a client-side relevance sort, and [n] is its
    position in the list the model was given. On any payload whose stored order was not
    already sorted the two disagreed, and the reader had no way to tell.
    """
    labels = ux["citations"]["labels"]
    # Displayed best-match first, so the higher-scoring chunk 2 leads — while keeping
    # the label the answer cites.
    assert [l["n"] for l in labels] == ["2", "1"], labels
    by_n = {l["n"]: l for l in labels}
    assert by_n["2"]["badge"].startswith("#2")
    assert "append" in by_n["2"]["text"]
    assert by_n["1"]["badge"].startswith("#1")
    assert "expire" in by_n["1"]["text"]


def test_a_citation_marker_opens_the_source_it_refers_to(ux):
    """Cross-referencing [2] against the inspector was a manual, error-prone read."""
    c = ux["citations"]
    assert sorted(c["refs"]) == ["[1]", "[2]"], c["refs"]
    assert c["inspectorOpened"], "clicking a citation did not open the inspector"
    assert c["clickedOpensRightChunk"], "clicking [2] did not expand source #2"


def test_citations_are_linked_on_a_live_streamed_answer(ux):
    """The order that matters. On a real turn the chunks arrive BEFORE the first token, so
    at that moment the bubble holds no [n] to link — and each stream frame rewrites the
    bubble's innerHTML, throwing away anything that had been linked. Linking only where the
    chunks arrive therefore works on a reopened conversation and silently does nothing on a
    live one, which is the case every user actually sees.
    """
    c = ux["citations_streamed"]
    assert c["linkedTooEarly"] == 0, "there was nothing to link yet — this is the setup"
    assert sorted(c["refs"]) == ["[1]", "[2]"], c["refs"]


def test_finalising_twice_leaves_exactly_one_button_per_marker(ux):
    """The backend emits a final token{done:true} AND a separate stream_end, so finalize is
    reached twice per answer. Two independent guards hold this — the finalize-once flag on
    the element, and the walker refusing to descend into a .cite-ref it already made — and
    the next test isolates the second one, which this test alone would not exercise.
    """
    c = ux["citations_streamed"]
    assert c["afterSecondFinalize"] == 2, c
    assert not c["nested"]


def test_linking_citations_twice_is_idempotent(ux):
    """Three call sites now reach _linkCitations (stream finalize, restore, version switch),
    so it has to survive a second pass over its own output. Without the walker excluding
    .cite-ref, the second pass reads the "[1]" inside the button it made and nests another
    button inside it.
    """
    c = ux["citations_idempotent"]
    assert c["count"] == 1, c
    assert not c["nested"], c
    assert c["text"] == "Cites [1] once.", c


def test_stepping_between_regenerated_versions_keeps_citations_live(ux):
    """showVersion rewrites the bubble's innerHTML, which discards every citation button in
    it. Without re-linking, stepping to another version of an answer — and back — leaves the
    markers as inert text, with no sign anything was lost.
    """
    c = ux["citations_version_switch"]
    assert c["before"] == 2, c
    assert c["afterBack"] == ["[1]"], c        # the first take cites one source
    assert sorted(c["afterForward"]) == ["[1]", "[2]"], c


def test_a_bracketed_number_inside_code_is_not_turned_into_a_citation(ux):
    """`arr[1]` is an array index, not a reference to source 1."""
    c = ux["citations_in_code"]
    assert c["refs"] == ["[1]"], c["refs"]
    assert c["codeUntouched"]


# ── keeping an answer ────────────────────────────────────────────────────────

def test_the_keep_dialog_offers_the_turn_back_for_editing(ux):
    """A "very good answer" is usually 95% good, and the 5% is what you would least want
    indexed forever — so this is a review step, not a one-click dump. The question is
    prefilled alongside the answer because it carries the wording a future search matches
    on, and the source toggle only appears when there were sources to offer.
    """
    p = ux["save_answer"]["prefilled"]
    assert p["question"] == "how long does a cached answer live?", p
    assert p["answer"] == "It lives for cache.ttl seconds [1].", p
    assert p["title"] == p["question"], "the title should default to the question"
    assert p["sourcesToggle"], p
    # The dedicated instance is offered even before it exists, and is the default.
    assert p["instance"] == "saved-answers", p
    assert p["options"][0] == "saved-answers" and "docs" in p["options"], p


def test_what_is_saved_is_the_edited_text_not_the_original(ux):
    """If the edit step did not feed the save, the review would be theatre."""
    body = json.loads(ux["save_answer"]["postedBody"])
    assert "It lives for cache.ttl seconds." in body["text"], body["text"]
    assert "[1]." not in body["text"], "the edit was discarded and the original saved"


def test_a_kept_answer_is_stored_with_its_question_and_its_provenance(ux):
    """Question first, because it is what a later search will match on. Sources appended,
    because a saved answer is retrieved and cited exactly like a document — without them
    it becomes an unattributable claim that looks like source material.
    """
    body = json.loads(ux["save_answer"]["postedBody"])
    assert body["text"].startswith("Q: how long does a cached answer live?\n\nA: "), body["text"]
    assert "Sources this answer was grounded in:" in body["text"]
    assert "- [1] settings.md" in body["text"] and "- [2] cache.md" in body["text"]


def test_a_kept_answer_is_labelled_as_an_answer_and_dated(ux):
    """The label is what the Documents view groups on and what the per-document delete
    addresses. It says `answer://` so a kept answer can never be mistaken, in the sources
    panel or the documents table, for something that came out of a real document."""
    import re as _re
    body = json.loads(ux["save_answer"]["postedBody"])
    assert _re.match(r"^answer://\d{4}-\d{2}-\d{2} how long does a cached answer live\?$",
                     body["source"]), body["source"]


def test_the_dedicated_instance_is_created_on_first_use_only(ux):
    """Ingesting into a name that does not exist would build the index but not its
    metadata — it would appear with no colour and no creation date, reading as something
    that had gone wrong."""
    created = json.loads(ux["save_answer"]["createdBody"])
    assert created["name"] == "saved-answers", created
    assert created["tags"] == ["saved-answers"], created
    # A newly created instance is on the default endpoint, which is what the create call
    # above asked for — so that is the endpoint the text must be posted to.
    assert ux["save_answer"]["postedTo"].startswith("/api/rag/saved-answers/ingest/text")
    assert "endpoint=default" in ux["save_answer"]["postedTo"], ux["save_answer"]["postedTo"]
    # ...and not again once it is there
    assert not ux["save_existing"]["created"], "the instance was re-created"
    assert ux["save_existing"]["posted"]


def test_a_kept_answer_is_written_to_the_endpoint_its_instance_lives_on(ux):
    """A RAG instance can live on a named Redis server. Posting without ?endpoint sends the
    text to the default one instead — the answer lands in an index nothing queries, and the
    save reports success."""
    url = ux["save_existing"]["postedUrl"]
    assert "endpoint=archive" in url, url


def test_an_ungrounded_answer_offers_no_source_toggle(ux):
    """There is nothing to append, and a switch that does nothing is worse than none."""
    assert ux["save_existing"]["noToggle"]


# ── model picker ─────────────────────────────────────────────────────────────
def test_the_picker_names_the_provider_and_the_model_without_being_opened(ux):
    """The strip it replaced showed the provider and nothing about the model; a
    reader had to look at a second control to learn what was answering."""
    assert ux["picker"]["closed"]["label"] == "gemma4:31b-mlx"
    assert ux["picker"]["closed"]["popHidden"] is True
    assert ux["picker"]["closed"]["expanded"] == "false"


def test_a_reachable_provider_gets_a_green_dot(ux):
    """Availability at the point of choice. The seven-button strip had two visual
    states for seven options and no per-provider status of any kind, so picking a
    provider with no key was the only way to find out it had none."""
    groups = ux["picker"]["open"]["groups"]
    assert len(groups) == 3, [g["text"] for g in groups]
    assert all(g["green"] for g in groups), groups


def test_providers_with_no_key_collapse_to_one_grey_row_rather_than_vanishing(ux):
    """Hiding them would mean an install with one key never learns the other six
    exist, and offers no route to setting one up from where you would look."""
    open_ = ux["picker"]["open"]
    assert open_["setupRow"] == "Claude · OpenAI · Qwen · Groq — no key, set up"
    assert open_["setupIsGreen"] is False
    assert open_["setupDot"] != ux["picker"]["open"]["groups"][0]["dot"]


def test_a_long_model_list_collapses_and_a_short_one_does_not(ux):
    """12 Gemini models is over the threshold; 2 Mistral and 2 Ollama are not."""
    assert ux["picker"]["open"]["collapsed"] == ["Show all 12 Gemini models"]


def test_the_model_in_use_is_marked_in_the_list(ux):
    assert ux["picker"]["open"]["selected"] == ["gemma4:31b-mlx"]


def test_choosing_a_model_from_another_provider_switches_both(ux):
    """Provider and model are one dependent choice; picking a model is the whole act."""
    picked = ux["picker"]["picked"]
    assert picked["provider"] == "mistral"
    assert picked["model"] == "mistral-large-latest"
    assert picked["popHidden"] is True


def test_vision_is_read_from_the_hosted_model_list(ux):
    """selectModel looked models up by display name while being called with the id,
    and no hosted route supplied a vision flag at all — so 📎 was disabled on every
    hosted provider regardless of the model. Both halves are checked here: a vision
    model enables it, a text-only one on the same provider turns it back off."""
    assert ux["picker"]["picked"]["attachEnabled"] is True
    assert ux["picker"]["picked"]["visionBadge"] != "none"
    assert ux["picker"]["textOnly"]["model"] == "codestral-latest"
    assert ux["picker"]["textOnly"]["attachEnabled"] is False


def test_a_provider_with_no_usable_model_does_not_leave_send_looking_ready(ux):
    """Switching to a provider whose list comes back empty used to leave a confident
    pill, an empty model list, and a Send button that only revealed the problem after
    it was pressed."""
    st = ux["picker_no_model"]
    assert st["model"] == ""
    assert "No Claude models found" in st["label"]
    assert st["sendDisabled"] is True
    assert st["attachDisabled"] is True


def test_escape_in_the_picker_closes_the_picker_and_nothing_underneath(ux):
    """The popover is not a .open/.visible layer, so it is not in _ESC_LAYERS. Without
    its own handling an Escape reached that loop and shut the panel behind it — the
    same double-close the search box carries a comment about."""
    esc = ux["picker_escape"]
    assert esc["under"] is True, "the layer underneath never opened; the test proves nothing"
    assert esc["opened"] is True, "the picker never opened; the test proves nothing"
    assert esc["first"]["pickerClosed"] is True
    assert esc["first"]["pinnedStillOpen"] is True, "Escape closed the layer underneath too"
    # ...and once the picker is closed, Escape resumes closing the layer stack
    assert esc["second"]["pinnedStillOpen"] is False


def test_send_stays_disabled_when_a_stream_ends_with_no_model(ux):
    """_setStreamingUI re-enabled Send blindly, so losing the model mid-answer left the
    button lit the moment the stream finished."""
    st = ux["picker_stream_end"]
    assert st["model"] == ""
    assert st["during"] is True
    assert st["after"] is True


def test_a_hostile_model_name_cannot_break_out_of_the_picker(ux):
    """Model ids and names come from the provider's API, and an OpenAI-compatible
    provider can be pointed at an arbitrary base URL. The id lands in a data attribute
    and the name in text, so a quote in one and a tag in the other are both tried."""
    x = ux["picker_xss"]
    assert x["pwned"] is False and x["pwned2"] is False
    assert x["imgs"] == 0 and x["scripts"] == 0
    # the payloads were actually rendered — otherwise this proves nothing
    assert '"><img src=x onerror=window.__pwned=1>' in x["ids"], x["ids"]
    assert "<script>window.__pwned2=1</script>" in x["labels"], x["labels"]
    assert "quote\'test" in x["ids"]


def test_a_hostile_id_still_round_trips_through_the_data_attribute(ux):
    """Escaping must survive the read back: an id that renders safely but returns
    mangled would select a model the provider does not have."""
    assert ux["picker_xss"]["ids"].count('"><img src=x onerror=window.__pwned=1>') == 1


def test_opening_the_picker_repeatedly_does_not_re_probe_every_provider(ux):
    """/api/status/<provider> is a live request to the provider that verifies the key,
    not a local read. Probing on every open would cost one API round trip per configured
    provider each time the menu is touched."""
    ttl = ux["picker_probe_ttl"]
    assert ttl["primed"] > 0, "the priming probe never ran; the rest proves nothing"
    assert ttl["repeats"] == 0, f"four opens re-probed {ttl['repeats']} times"
    # ...but an explicit refresh must still see a key that was just added
    assert ttl["forced"] == ttl["primed"]


def test_refreshing_another_providers_models_leaves_the_active_list_alone(ux):
    """Each provider card has its own Refresh Models button. The old loaders rewrote the
    top-bar dropdown directly, so refreshing OpenAI while Mistral was active left the bar
    offering models the running provider cannot use."""
    r = ux["picker_refresh"]
    assert r["before"]["ids"] == ["gemma4:31b-mlx", "llama3:latest"]
    assert r["other"] == r["before"], "a non-active provider's refresh changed the active list"


def test_refreshing_the_active_provider_does_pick_up_a_new_model(ux):
    """The other half — otherwise the non-clobber test above would pass on a button
    that simply does nothing."""
    assert ux["picker_refresh"]["own"]["ids"] == ["newly-pulled:7b"]
    assert ux["picker_refresh"]["own"]["model"] == "newly-pulled:7b"


def test_the_no_key_row_opens_provider_settings(ux):
    """It is the only route from the picker to setting a provider up."""
    st = ux["picker_setup_link"]
    assert not st.get("noRow"), "the no-key row was not rendered"
    assert st["settingsOpen"] is True
    assert st["tab"] == "tab-providers"
    assert st["pickerClosed"] is True


def test_the_pickers_accessible_name_carries_the_live_selection(ux):
    """A static aria-label would replace the button's content as its accessible name, so
    neither the model nor the provider's reachability would reach a screen reader — the
    dot, separator and caret are all aria-hidden."""
    n = ux["picker_a11y_name"]
    assert n["isStatic"] is False, "the accessible name did not change with the selection"
    assert "Ollama" in n["ollama"] and "gemma4:31b-mlx" in n["ollama"], n["ollama"]
    assert "Gemini" in n["gemini"] and "gemini-2.5-pro" in n["gemini"], n["gemini"]
    # status is colour-only on the dot, so it must be spelled out here
    assert "Reachable" in n["ollama"], n["ollama"]


def test_the_picker_paints_above_the_answer_cards(ux):
    """#topbar carries backdrop-filter, which makes it a stacking context — the popover
    cannot escape it however high its own z-index is, and #chat-area follows in the DOM.
    Without a z-index on #topbar the menu rendered underneath answer cards and tables."""
    st = ux["picker_stacking"]
    # With the menu hidden, those points land on conversation content — so the menu has
    # something real to beat. Without this the hit-test could pass over empty page.
    assert st["inChatArea"] == st["sampled"], \
        f"only {st['inChatArea']}/{st['sampled']} sample points sat over chat: {st['beneath']}"
    assert st["topbarPosition"] != "static", "z-index needs a positioned element"
    assert st["topbarZ"] != "auto", "without a z-index the popover cannot leave #topbar"
    assert st["allInsidePopover"] is True, f"something painted over the menu: {st['topmost']}"


def test_the_provider_default_is_marked_and_sorted_near_the_top(ux):
    """On Gemini and Mistral the default is the free-tier model — the one that costs
    nothing to try — so it should not be buried behind "Show all N"."""
    d = ux["picker_default"]
    assert d["markedIds"] == ["gemini-2.5-pro"], d["markedIds"]
    assert d["badgeText"] == "default"
    # The model in use sorts first on its own rule, so it must NOT be the default here —
    # otherwise rank 0 masks the default rule and this proves nothing.
    assert d["inUse"] == "gemini-2.5-flash", d["inUse"]
    assert d["order"][0] == "gemini-2.5-flash", d["order"]     # in use
    assert d["order"][1] == "gemini-2.5-pro", d["order"]       # the default, above the alias


def test_the_default_model_is_a_different_colour_from_the_rest(ux):
    """A badge alone is easy to miss in a list; the colour is what carries at a glance."""
    d = ux["picker_default"]
    assert d["defaultColour"] is not None, "nothing was marked as the default"
    assert d["plainColour"] is not None, "no plain row to compare against"
    assert d["defaultColour"] != d["plainColour"], \
        f"default and ordinary rows are both {d['defaultColour']}"


def test_a_grouped_citation_marker_links_every_source_it_names(ux):
    """"[3, 4]" is as natural for a model as "[3] [4]", and matching only a lone number
    left the grouped form as dead text — visible as a citation, but opening nothing."""
    g = ux["citations_grouped"]
    assert g["refs"] == ["[3]", "[4]", "[1]", "[2]"], g["refs"]
    assert g["targets"] == ["3", "4", "1", "2"], g["targets"]


def test_a_marker_outside_the_answers_sources_is_left_as_text(ux):
    """[9] with four sources is not a citation into this answer; turning it into a
    button would offer to open a passage that does not exist."""
    assert ux["citations_grouped"]["keptOutOfRange"] is True


def test_linking_citations_does_not_disturb_the_prose(ux):
    """The linker rewrites text nodes in place, so a bug here silently eats words."""
    assert ux["citations_grouped"]["prose"] == (
        "A symlink holds a pathname [3] [4]. It is resolved at open time [1] [2]. "
        "Out of range [9] stays text.")


# ── single-row top bar ───────────────────────────────────────────────────────
def test_the_top_bar_is_one_row_at_every_width(ux):
    """It was two rows, and the second wrapped again as the title and token counter
    grew — 99px on a fresh session, 198px in use, every pixel taken from the chat."""
    bars = ux["topbar_single_row"]
    for w, m in bars.items():
        assert m["rows"] == 3, f"{w}px: top bar has {m['rows']} direct children, expected 3"
        assert m["h"] <= 60, f"{w}px: top bar is {m['h']}px — it has wrapped to a second line"


def test_the_top_bar_never_overflows_its_own_width(ux):
    """The controls do not wrap any more, so if they stop giving way they run off the
    edge instead — silently, because nothing clips them."""
    over = {w: m for w, m in ux["topbar_single_row"].items() if m["overflows"]}
    assert not over, f"top bar overflows at: {sorted(over)}"


def test_the_top_bar_height_does_not_change_with_use(ux):
    """A long session title and a seven-digit token count used to add a whole row."""
    heights = {m["h"] for m in ux["topbar_single_row"].values()}
    assert len(heights) == 1, f"height varies by width: {heights}"


def test_clear_chat_lives_in_the_sidebar_not_the_top_bar(ux):
    c = ux["clear_chat_home"]
    assert c["inTopbar"] is False, "the trash button is still in the top bar"
    assert c["inSidebar"] is True
    assert "Clear Chat" in c["label"], c["label"]
    # ...next to the other actions that operate on the current conversation
    order = c["footerOrder"]
    assert order.index([x for x in order if "Clear Chat" in x][0]) == \
           order.index([x for x in order if "Export Chat (.txt)" in x][0]) + 1, order


# ── function plot definitions ────────────────────────────────────────────────
def test_a_plot_definition_whose_argument_is_not_x_still_plots(ux):
    """log(N) = -D * log(x) is how a log-log relation is written. Only a literal "(x)"
    was accepted as a definition, so the whole line was treated as the expression —
    mathjs then read it as *defining* a function called log, which evaluates to a
    function rather than a number, and the card died with "no finite values"."""
    p = ux["plot_defs"]
    assert p["ok"] is True, p["error"]
    assert p["series"] == 4, f"expected four curves, drew {p['series']}"
    assert p["distinctStrokes"] == 4, "the curves are not separately coloured"


def test_the_plot_legend_keeps_the_written_names(ux):
    """A legend reading f1..f4 would not say which fractal is which."""
    assert ux["plot_defs"]["labels"] == [
        "log(N_line)", "log(N_sq)", "log(N_Sierpinski)", "log(N_Koch)"], ux["plot_defs"]["labels"]


@pytest.mark.parametrize("shape", ["plainY", "fOfX", "bareExpr"])
def test_the_plot_shapes_that_already_worked_still_work(ux, shape):
    """Broadening what counts as a definition must not swallow anything else."""
    assert ux["plot_defs"][shape] is True, shape


def test_an_undefined_symbol_still_names_itself(ux):
    """The specific diagnostic must survive the broader match."""
    assert "'a' is not defined" in ux["plot_defs"]["undefSym"], ux["plot_defs"]["undefSym"]


def test_an_expression_that_is_not_a_number_says_so(ux):
    """The net under the fix: mathjs can compile and evaluate without throwing yet hand
    back a function. That used to surface as the generic "no finite values"."""
    err = ux["plot_defs"]["nonNumeric"]
    assert "not a number" in err, err
    assert "function" in err, err


def test_the_non_number_message_names_the_type_it_got(ux):
    """"not a number" alone leaves the reader guessing; the type is the clue."""
    err = ux["plot_defs"]["nonNumericMatrix"]
    assert "Matrix" in err and "not a number" in err, err
