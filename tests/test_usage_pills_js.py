# SPDX-License-Identifier: AGPL-3.0-or-later
"""updateTokenCounter, run as real JavaScript: real provider usage beats the
chars/4 estimate, a measured turn covers its whole exchange (the user turn that
produced it must not be double-counted), estimated turns keep the "~", and cost
comes from the pricing table via each turn's model."""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _fn() -> str:
    """updateTokenCounter plus _tokenCost, which it calls for the per-turn pricing
    lookup (the same helper the Analytics token table uses, so the pill and the table
    can never drift apart on how a turn is priced)."""
    html = _INDEX.read_text(encoding="utf-8")
    return "\n".join(extract_js_function(html, n) for n in
                     ("_tokenCost", "updateTokenCounter"))


def _run(messages, pricing) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = f"""
const texts={{}};
const document={{getElementById:id=>({{set textContent(v){{texts[id]=v;}},
  get textContent(){{return texts[id];}}, style:{{}}}})}};
const S={{sessionId:'x',config:{{pricing:{json.dumps(pricing)}}},
  sessions:{{x:{{messages:{json.dumps(messages)}}}}}}};
{_fn()}
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
    ], {"gpt-4o": {"in": 2.5, "out": 10.0}})
    # prompt covers the whole exchange — the 100-token user estimate must NOT be added
    assert t["tok-in"] == "↑ 1,200"
    assert t["tok-out"] == "↓ 300"
    assert t["tok-total"] == "Σ 1,500"
    # 1200/1e6*2.5 + 300/1e6*10 = 0.003 + 0.003 = 0.006
    assert t["tok-cost"] == "$0.0060"


def test_mixed_real_and_estimated_turns_keep_the_tilde():
    t = _run([
        {"role": "user", "content": "q" * 400},          # estimated (100 tok)
        {"role": "assistant", "content": "a" * 800},     # estimated (200 tok) — no usage
        {"role": "user", "content": "w" * 400},          # covered by the next turn
        {"role": "assistant", "content": "b" * 4000,
         "meta": {"model": "m", "usage": {"prompt": 500, "completion": 50}}},
    ], {})
    assert t["tok-in"] == "↑ ~600"    # 100 estimated + 500 real
    assert t["tok-out"] == "↓ ~250"   # 200 estimated + 50 real
    assert t["tok-total"] == "Σ ~850"


def test_cached_tokens_are_counted_and_priced_separately():
    t = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a",
         "meta": {"model": "claude-sonnet-4-6",
                  "usage": {"prompt": 100, "completion": 200, "cached": 1000}}},
    ], {"claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cached_in": 0.30}})
    assert t["tok-in"] == "↑ 1,100"   # prompt + cached both count as input volume
    # 100/1e6*3 + 1000/1e6*0.30 + 200/1e6*15 = 0.0003+0.0003+0.003 = 0.0036
    assert t["tok-cost"] == "$0.0036"


def test_unknown_model_shows_tokens_but_no_cost():
    t = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a",
         "meta": {"model": "qwen-plus", "usage": {"prompt": 10, "completion": 5}}},
    ], {})
    assert t["tok-in"] == "↑ 10"
    assert "tok-cost" not in t or t.get("tok-cost") in (None, "",)  # never written
