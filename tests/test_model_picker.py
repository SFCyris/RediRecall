# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model list a provider offers, and the vision flag attached to each entry.

Two shipped defects are guarded here. Gemini's list was filtered on
`startswith("gemini")` alone, so 37 entries reached the picker of which 21 could not
hold a conversation — 9 of those cannot be prompted at all. Mistral's route carried a
comment claiming it returned chat models only while nothing filtered it, so embedding,
OCR, moderation and audio models were offered as chat models too.

The vision flag is the other half: `selectModel` gates the 📎 attach button on it, and
no hosted provider ever supplied one, so attaching an image was impossible on every
provider except Ollama regardless of the model's real capability.
"""
import pytest

from redirecall import providers


# ── Gemini: capability first, purpose second ─────────────────────────────────
_GENERATE = ("generateContent",)


@pytest.mark.parametrize("mid", [
    "gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview",
    "gemini-3.1-pro-preview", "gemini-flash-latest", "gemini-pro-latest",
    "gemini-3.7-flash", "gemini-omni-flash-preview",
])
def test_a_conversational_gemini_model_is_kept(mid):
    assert providers._gemini_is_chat(mid, _GENERATE) is True


@pytest.mark.parametrize("mid,actions", [
    # (1) no generateContent — the API's own answer, and the only authoritative gate
    ("gemini-embedding-001",                  ("embedContent", "countTokens")),
    ("gemini-embedding-2",                    ("embedContent",)),
    ("gemini-2.5-flash-native-audio-latest",  ("bidiGenerateContent",)),
    ("gemini-3.1-flash-live-preview",         ("bidiGenerateContent",)),
    ("gemini-3.5-live-translate-preview",     ("bidiGenerateContent",)),
    # (2) generateContent, but not a text chat model
    ("gemini-2.5-flash-preview-tts",          _GENERATE),
    ("gemini-3-pro-image",                    _GENERATE),
    ("gemini-3.1-flash-tts-preview",          _GENERATE),
    ("gemini-robotics-er-2-preview",          _GENERATE),
    ("gemini-2.5-computer-use-preview-10-2025", _GENERATE),
])
def test_a_non_conversational_gemini_model_is_dropped(mid, actions):
    assert providers._gemini_is_chat(mid, actions) is False


def test_the_capability_gate_outranks_the_name_gate():
    """A plain chat name with no generateContent is still dropped: the name heuristic
    is a second filter, never a way back in."""
    assert providers._gemini_is_chat("gemini-2.5-flash", ()) is False
    assert providers._gemini_is_chat("gemini-2.5-flash", None) is False


# ── Mistral: the API reports capabilities; use them ──────────────────────────
_MISTRAL_SAMPLE = [
    {"id": "mistral-large-latest", "max_context_length": 128000,
     "capabilities": {"completion_chat": True, "vision": True}},
    {"id": "codestral-latest", "max_context_length": 256000,
     "capabilities": {"completion_chat": True, "completion_fim": True, "vision": False}},
    {"id": "mistral-embed", "capabilities": {"completion_chat": False, "embed": True}},
    {"id": "mistral-ocr-latest", "capabilities": {"ocr": True}},
    {"id": "voxtral-mini-tts-latest", "capabilities": {"audio_speech": True}},
    {"id": "mistral-moderation-2603", "capabilities": {"moderation": True}},
    {"id": "no-capabilities-at-all"},
]


def test_mistral_keeps_only_chat_capable_models():
    got = [m["id"] for m in providers.filter_mistral_models(_MISTRAL_SAMPLE)]
    assert got == ["codestral-latest", "mistral-large-latest"]


def test_mistral_vision_comes_from_the_api_not_the_name_table():
    """`mistral-large-latest` is not in _VISION_PREFIXES, so a name-only answer would
    call it text-only. The capability object says otherwise and must win."""
    by_id = {m["id"]: m for m in providers.filter_mistral_models(_MISTRAL_SAMPLE)}
    assert providers.supports_vision("mistral-large-latest") is False
    assert by_id["mistral-large-latest"]["vision"] is True
    assert by_id["codestral-latest"]["vision"] is False


def test_mistral_carries_the_context_length_through():
    by_id = {m["id"]: m for m in providers.filter_mistral_models(_MISTRAL_SAMPLE)}
    assert by_id["codestral-latest"]["context"] == 256000


def test_mistral_filter_survives_junk():
    assert providers.filter_mistral_models([]) == []
    assert providers.filter_mistral_models(None) == []
    assert providers.filter_mistral_models([{"capabilities": {"completion_chat": True}}]) == []


# ── The vision table ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("mid", [
    "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-nano",
    "o1", "o3", "o4-mini", "pixtral-12b-2409", "gemini-2.5-pro",
])
def test_a_vision_capable_hosted_model_is_flagged(mid):
    assert providers.supports_vision(mid) is True


@pytest.mark.parametrize("mid", [
    "o3-mini",                    # text-only sibling of o3 — must not match an "o3" prefix
    "mistral-small-latest", "open-mistral-nemo", "codestral-latest",
    "llama-3.3-70b-versatile", "mixtral-8x7b-32768", "gemma2-9b-it",
    "qwen-plus", "qwen2.5-72b-instruct", "",
])
def test_a_text_only_model_is_not_flagged(mid):
    assert providers.supports_vision(mid) is False


def test_o3_mini_is_excluded_by_exact_match_not_prefix():
    """o3 has vision, o3-mini does not. If the table ever moves "o3" from the exact set
    into the prefix tuple, o3-mini silently gains a 📎 button that errors on send."""
    assert "o3" in providers._VISION_EXACT
    assert not any("o3".startswith(p) or p.startswith("o3") for p in providers._VISION_PREFIXES)


# ── stamp_vision ─────────────────────────────────────────────────────────────
def test_stamp_vision_never_mutates_the_shared_static_lists():
    """The hosted fetchers return the module-level *_STATIC lists on fallback. Stamping
    in place would give those constants a `vision` key for the life of the process."""
    before = [dict(m) for m in providers.CLAUDE_MODELS]
    out = providers.stamp_vision(providers.CLAUDE_MODELS)
    assert providers.CLAUDE_MODELS == before
    assert all("vision" not in m for m in providers.CLAUDE_MODELS)
    assert all(m["vision"] is True for m in out)


def test_stamp_vision_defers_to_a_flag_that_is_already_there():
    got = providers.stamp_vision([{"id": "mistral-large-latest", "vision": True},
                                  {"id": "mistral-small-latest"}])
    assert [m["vision"] for m in got] == [True, False]


def test_stamp_vision_marks_every_entry():
    """A missing key reads as False in the frontend, so an unstamped entry is an
    attach button that stays disabled for no stated reason."""
    got = providers.stamp_vision(providers.GROQ_MODELS_STATIC)
    assert all("vision" in m for m in got)


# ── every model route, not just the ones with a test each ────────────────────
@pytest.mark.parametrize("route", [
    "api_claude_models", "api_openai_models", "api_qwen_models",
    "api_mistral_models", "api_groq_models", "api_gemini_models",
])
def test_every_hosted_model_route_flags_vision_on_every_entry(route, monkeypatch):
    """The frontend reads a missing key as False, so an unstamped route is an attach
    button that stays disabled for no stated reason. Run with no API keys, which is the
    path that returns the static fallback lists — the one most easily left unstamped."""
    import asyncio
    import inspect

    from redirecall import routes_settings, state

    monkeypatch.setattr(state, "_config", {}, raising=False)
    fn = getattr(routes_settings, route)
    out = asyncio.run(fn()) if inspect.iscoroutinefunction(fn) else fn()
    assert out, f"{route} returned nothing"
    missing = [m.get("id") for m in out if "vision" not in m]
    assert not missing, f"{route} left these unflagged: {missing}"
    assert all(isinstance(m["vision"], bool) for m in out)


def test_mistral_uses_the_curated_label_where_there_is_one():
    """The live endpoint returns bare ids. Building the display name from the id alone
    discarded the "(free tier)" hint the static list carries — the very thing that marks
    which Mistral models cost nothing to run."""
    got = {m["id"]: m["name"] for m in providers.filter_mistral_models([
        {"id": "mistral-small-latest", "capabilities": {"completion_chat": True}},
        {"id": "some-future-model",    "capabilities": {"completion_chat": True}},
    ])}
    assert got["mistral-small-latest"] == "Mistral Small (free tier)"
    assert got["some-future-model"] == "some-future-model"   # unknown ids keep the id
