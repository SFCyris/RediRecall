# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the token/perf optimizations (T1-1..5, T2-2).

These guard the invariants an adversarial QA pass found unprotected — above all
that the token-budgeted history window never hands a provider an assistant-first
message list (Anthropic and Gemini reject "first message must use the user role"),
which was a real regression the flat ``[-10:]`` window never had. Pure functions,
no Redis needed.
"""
from redirecall import sessions, config, cache, constants, state


def _alt(n: int, size: int = 400) -> list:
    """n alternating user/assistant messages, each `size` chars — the shape a real
    session always has (starts user, ends assistant on even n)."""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size}
            for i in range(n)]


# ── T1-4: history_window must never lead with an assistant turn ──────────────
def test_history_window_never_leads_with_assistant():
    # A single huge assistant answer over budget → empty window (so the built
    # message list is just [system, user], which every provider accepts).
    w = sessions.history_window(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x" * 20000}], 3000)
    assert not (w and w[0]["role"] == "assistant"), "window must not start with assistant"
    # Odd-boundary truncation across many verbose pairs → still user-first (or empty)
    # for every budget, including ones that land on an odd count.
    for budget in (100, 1000, 3000, 8000, 20000):
        w = sessions.history_window(_alt(12, 2000), budget)
        assert not w or w[0]["role"] == "user", (budget, [m["role"] for m in w])


def test_history_window_keeps_recent_and_respects_caps():
    msgs = _alt(20, 400)
    w = sessions.history_window(msgs, 3000)
    assert w[-1] is msgs[-1]                    # the newest turn is always kept
    assert w[0]["role"] == "user"
    # budget <= 0 falls back to the hard message cap (20), NOT an unbounded transcript
    assert len(sessions.history_window(_alt(40, 10), 0)) == 20


def test_approx_tokens_ignores_image_data():
    # A vision turn's huge base64 image URI must not be counted (it is never
    # resent from history) — otherwise one image would evict all real conversation.
    turn = [{"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200000}}]
    assert sessions._approx_tokens(turn) < 10


# ── T1-1: visual-authoring section is gated, core guidance is not ────────────
def test_visual_gating_drops_section_keeps_core():
    saved = dict(state._config)
    try:
        state._config["base_instruction"] = constants.DEFAULT_BASE_INSTRUCTION
        state._config["visual_instructions"] = True
        full = config.compose_system_prompt(None)
        state._config["visual_instructions"] = False
        core = config.compose_system_prompt(None)
        assert constants.VISUAL_SECTION_MARKER in full
        assert constants.VISUAL_SECTION_MARKER not in core          # section dropped
        assert "Answer clearly and concisely" in core               # core retained
        assert len(core) < len(full) // 2                           # real token cut
        # A selected template is still appended after gating (additive semantics).
        assert config.compose_system_prompt("You are a pirate.").endswith("You are a pirate.")
    finally:
        state._config.clear()
        state._config.update(saved)


# ── T1-3: cache-key normalization folds case/whitespace, nothing more ────────
def test_normalize_query_is_case_whitespace_only():
    assert cache._normalize_query("  Enable  X? ") == cache._normalize_query("enable x?")
    # Opposite-intent queries must stay distinct so the cache never serves a
    # wrong answer — normalization folds case and whitespace, never words.
    assert cache._normalize_query("enable feature") != cache._normalize_query("disable feature")
