# SPDX-License-Identifier: AGPL-3.0-or-later
"""updateTokenCounter, run as real JavaScript: real provider usage beats the
chars/4 estimate, a measured turn covers its whole exchange (the user turn that
produced it must not be double-counted), and estimated turns keep the "~".

The pills report tokens only. There is no cost figure: the app never had a rate it
could stand behind — the table was a handful of hand-written constants that no
provider supplied and nothing revalidated, so a displayed dollar amount was as
likely to be wrong as right.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _run(messages) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not available")
    fn = extract_js_function(_INDEX.read_text(encoding="utf-8"), "updateTokenCounter")
    js = f"""
const texts={{}};
const document={{getElementById:id=>({{set textContent(v){{texts[id]=v;}},
  get textContent(){{return texts[id];}}, style:{{}}}})}};
const S={{sessionId:'x',config:{{}},
  sessions:{{x:{{messages:{json.dumps(messages)}}}}}}};
{fn}
updateTokenCounter();
console.log(JSON.stringify(texts));
"""
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_real_usage_shows_exact_counts_without_tilde():
    t = _run([
        {"role": "user", "content": "q" * 400},
        {"role": "assistant", "content": "a" * 4000,
         "meta": {"model": "gpt-4o", "usage": {"prompt": 1200, "completion": 300}}},
    ])
    # prompt covers the whole exchange — the 100-token user estimate must NOT be added
    assert t["tok-in"] == "↑ 1,200"
    assert t["tok-out"] == "↓ 300"
    assert t["tok-total"] == "Σ 1,500"


def test_mixed_real_and_estimated_turns_keep_the_tilde():
    t = _run([
        {"role": "user", "content": "q" * 400},          # estimated (100 tok)
        {"role": "assistant", "content": "a" * 800},     # estimated (200 tok) — no usage
        {"role": "user", "content": "w" * 400},          # covered by the next turn
        {"role": "assistant", "content": "b" * 4000,
         "meta": {"model": "m", "usage": {"prompt": 500, "completion": 50}}},
    ])
    assert t["tok-in"] == "↑ ~600"    # 100 estimated + 500 real
    assert t["tok-out"] == "↓ ~250"   # 200 estimated + 50 real
    assert t["tok-total"] == "Σ ~850"


def test_cached_and_cache_write_tokens_count_as_input_volume():
    """Providers bill them separately, but they are all prompt tokens the model read,
    so the input pill has to include them or it under-reports the exchange."""
    t = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a",
         "meta": {"model": "claude-sonnet-4-6",
                  "usage": {"prompt": 100, "completion": 200,
                            "cached": 1000, "cache_write": 400}}},
    ])
    assert t["tok-in"] == "↑ 1,500"    # 100 + 1000 + 400
    assert t["tok-out"] == "↓ 200"


def test_no_cost_pill_is_ever_written():
    """Guard against the cost figure coming back by accident: the rates behind it were
    never sourced from the provider or the user, so any amount shown was a guess."""
    t = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a",
         "meta": {"model": "gpt-4o", "usage": {"prompt": 1000, "completion": 1000}}},
    ])
    assert "tok-cost" not in t, t
    assert not any("$" in str(v) for v in t.values()), t
