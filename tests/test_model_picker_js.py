# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model picker's pure logic, run as real JavaScript.

Three things the browser probe cannot pin precisely: the shape normalisation that
made the vision flag reachable at all, the ordering that decides what "Show all"
hides, and the preference round-trip that survives a reload.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _run(body: str, fns: tuple, state: dict | None = None) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in fns)
    # _mpRank consults _mpDefaultFor, which reads PROVIDER_META and S.config.
    js = f"""
const S = {json.dumps(state or {})};
const PROVIDER_META = {{gemini:{{fallback:'gemini-3-flash-preview'}},
                        mistral:{{fallback:'mistral-small-latest'}},
                        ollama:{{fallback:''}}}};
{src}
{body}
"""
    r = run_node(js)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── shape normalisation ──────────────────────────────────────────────────────
def test_ollama_models_key_on_name_and_keep_their_vision_flag():
    out = _run("""
      console.log(JSON.stringify(_normalizeModels('ollama', [
        {name:'gemma4:31b-mlx', size:18e9, vision:true},
        {name:'llama3:latest',  size:4.7e9, vision:false}])));
    """, ("_normalizeModels",))
    assert out == [
        {"id": "gemma4:31b-mlx", "label": "gemma4:31b-mlx", "vision": True,  "size": 18e9},
        {"id": "llama3:latest",  "label": "llama3:latest",  "vision": False, "size": 4.7e9},
    ]


def test_hosted_models_key_on_id_with_name_as_the_display_label():
    """The bug this fixes: hosted lists carry {id, name} where `name` is a display
    label, and selectModel looked models up by `name` while being called with the id.
    Every lookup missed, so `m?.vision` was undefined and 📎 stayed disabled."""
    out = _run("""
      console.log(JSON.stringify(_normalizeModels('claude', [
        {id:'claude-sonnet-4-6', name:'Claude Sonnet 4.6', context:200000, vision:true}])));
    """, ("_normalizeModels",))
    assert out == [{"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
                    "vision": True, "context": 200000}]


def test_a_missing_vision_key_reads_as_false_not_undefined():
    out = _run("""
      console.log(JSON.stringify(_normalizeModels('groq', [{id:'llama-3.3-70b-versatile'}])));
    """, ("_normalizeModels",))
    assert out[0]["vision"] is False


def test_entries_without_an_id_are_dropped():
    """A blank id would render a nameless row that selects an empty model."""
    out = _run("""
      console.log(JSON.stringify(_normalizeModels('openai',
        [{id:'gpt-4o'}, {name:'no id here'}, {}])));
    """, ("_normalizeModels",))
    assert [m["id"] for m in out] == ["gpt-4o"]


def test_a_non_list_response_yields_an_empty_list():
    out = _run("""
      console.log(JSON.stringify([_normalizeModels('openai', null),
                                  _normalizeModels('openai', {error:'nope'})]));
    """, ("_normalizeModels",))
    assert out == [[], []]


# ── ordering behind "Show all" ───────────────────────────────────────────────
_GEMINI = ["gemini-2.5-flash", "gemini-3.1-pro-preview", "gemini-flash-latest",
           "gemini-2.5-computer-use-preview-10-2025", "gemini-pro-latest",
           "gemini-3.7-flash"]


def test_the_selected_model_always_sorts_first():
    """It is the one entry that must never end up hidden behind "Show all N"."""
    out = _run(f"""
      const ms = {json.dumps(_GEMINI)}.map(id => ({{id}}));
      console.log(JSON.stringify(ms.sort((a,b)=>_mpRank(a,'gemini')-_mpRank(b,'gemini')).map(m=>m.id)));
    """, ("_mpRank", "_mpDefaultFor"), {"currentModel": "gemini-3.7-flash", "config": {}})
    assert out[0] == "gemini-3.7-flash"


def test_latest_aliases_outrank_plain_names_which_outrank_dated_snapshots():
    out = _run(f"""
      const ms = {json.dumps(_GEMINI)}.map(id => ({{id}}));
      console.log(JSON.stringify(ms.map(m=>[m.id,_mpRank(m,'gemini')])));
    """, ("_mpRank", "_mpDefaultFor"), {"currentModel": "none-of-them", "config": {}})
    rank = dict(out)
    assert rank["gemini-flash-latest"] == 2
    assert rank["gemini-pro-latest"] == 2
    assert rank["gemini-2.5-flash"] == 3
    assert rank["gemini-3.1-pro-preview"] == 3
    # a pinned dated snapshot is nearly always a copy of a model already listed
    assert rank["gemini-2.5-computer-use-preview-10-2025"] == 4
    assert rank["gemini-flash-latest"] < rank["gemini-2.5-flash"] < rank["gemini-2.5-computer-use-preview-10-2025"]


@pytest.mark.parametrize("mid", ["mistral-small-2506", "codestral-2508",
                                 "gemini-2.5-flash-native-audio-preview-09-2025"])
def test_a_date_stamped_id_is_recognised_as_a_snapshot(mid):
    out = _run(f'console.log(JSON.stringify(_mpRank({{id:{json.dumps(mid)}}},"gemini")));',
               ("_mpRank", "_mpDefaultFor"), {"currentModel": "", "config": {}})
    assert out == 4


# ── preferences round trip ───────────────────────────────────────────────────
def _prefs_env(state: dict) -> str:
    # _LS_PREFS is a module-level const the extractor does not pull in with the
    # functions. Without it both calls raise ReferenceError straight into their own
    # catch blocks and the round trip silently "passes" as {}.
    return f"""
const _LS_PREFS = 'rr.prefs';
const store = {{}};
const localStorage = {{getItem:k=>k in store?store[k]:null,
                       setItem:(k,v)=>{{store[k]=String(v);}}}};
Object.assign(S, {json.dumps(state)});
function _activeRagKey(){{return S.activeRag?S.activeRag.name+'§'+(S.activeRag.ep||'default'):'';}}
"""


@pytest.mark.parametrize("state,expect", [
    ({"provider": "gemini", "currentModel": "gemini-2.5-pro", "noRag": False,
      "ragAllMode": False, "activeRag": {"name": "Ubuntu", "ep": "default"}},
     {"provider": "gemini", "model": "gemini-2.5-pro", "ragScope": "Ubuntu§default"}),
    ({"provider": "ollama", "currentModel": "llama3", "noRag": True,
      "ragAllMode": False, "activeRag": {"name": "Ubuntu", "ep": "default"}},
     {"provider": "ollama", "model": "llama3", "ragScope": "__none__"}),
    ({"provider": "mistral", "currentModel": "codestral-latest", "noRag": False,
      "ragAllMode": True, "activeRag": {"name": "Ubuntu", "ep": "default"}},
     {"provider": "mistral", "model": "codestral-latest", "ragScope": "__all__"}),
])
def test_every_scope_state_round_trips(state, expect):
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in ("_loadPrefs", "_savePrefs"))
    js = f"const S = {{}};\n{_prefs_env(state)}\n{src}\n_savePrefs();\nconsole.log(JSON.stringify(_loadPrefs()));"
    r = run_node(js)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == expect


def test_unreadable_storage_never_throws():
    """Private mode and quota errors must not take the picker down with them."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in ("_loadPrefs", "_savePrefs"))
    js = f"""
const _LS_PREFS = 'rr.prefs';
const S = {{provider:'ollama',currentModel:'x',noRag:false,ragAllMode:false,activeRag:null}};
const localStorage = {{getItem:()=>{{throw new Error('denied');}},
                       setItem:()=>{{throw new Error('quota');}}}};
function _activeRagKey(){{return '';}}
{src}
_savePrefs();
console.log(JSON.stringify(_loadPrefs()));
"""
    r = run_node(js)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {}


# ── the provider's default ───────────────────────────────────────────────────
def test_the_configured_default_outranks_everything_but_the_model_in_use():
    """On Gemini and Mistral the default is the free-tier model, so it is the one worth
    pointing at. It must not be buried behind "Show all N"."""
    out = _run("""
      const ms = ['gemini-flash-latest','gemini-3-flash-preview','gemini-2.5-pro']
                   .map(id => ({id}));
      console.log(JSON.stringify(ms.map(m => [m.id, _mpRank(m, 'gemini')])));
    """, ("_mpRank", "_mpDefaultFor"), {"currentModel": "gemini-2.5-pro", "config": {}})
    rank = dict(out)
    assert rank["gemini-2.5-pro"] == 0            # in use
    assert rank["gemini-3-flash-preview"] == 1    # the provider fallback
    assert rank["gemini-flash-latest"] == 2       # an alias, but not the default


def test_a_configured_model_overrides_the_built_in_fallback():
    out = _run("""
      console.log(JSON.stringify([_mpDefaultFor('gemini'), _mpDefaultFor('mistral')]));
    """, ("_mpDefaultFor",), {"config": {"gemini": {"model": "gemini-2.5-pro"}}})
    assert out == ["gemini-2.5-pro", "mistral-small-latest"]


def test_an_unknown_provider_has_no_default():
    out = _run("console.log(JSON.stringify(_mpDefaultFor('nope')));",
               ("_mpDefaultFor",), {"config": {}})
    assert out == ""
