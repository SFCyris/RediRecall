# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Analytics "Token Usage" card, run as real JavaScript.

The card is the only surface that answers "how many tokens have I actually burned"
across every session. It reads GET /api/usage, whose fields are disjoint token classes
(fresh input / cache read / cache write / output) priced at different rates, so the
column semantics and the partial-cost signalling are the things worth pinning down.

Extracted from index.html and run under node — pure string work, no browser.
"""
import json
import pathlib
import re
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _fns() -> str:
    """The card renderer plus the helpers it calls. escHtml is pulled in too — it is the
    only thing standing between a model-supplied model name and the Analytics DOM."""
    html = _INDEX.read_text(encoding="utf-8")
    # Brace-balanced: slicing to the next line-initial '}' swallowed the following function
    # whenever the target was a one-liner (_fmtCost), so the payload defined
    # _tokenUsageCardHTML twice and escHtml dragged in five unrelated helpers.
    return "\n".join(extract_js_function(html, n) for n in
                     ("_tokenCost", "_fmtCost", "escHtml", "_tokenUsageCardHTML"))


def _render(usage, pricing=None) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = (f"const S={{config:{{pricing:{json.dumps(pricing or {})}}}}};\n"
          + _fns()
          + f"\nconsole.log(JSON.stringify(_tokenUsageCardHTML({json.dumps(usage)})));\n")
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _cells(html_str: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", html_str, re.S)]


def test_empty_tally_renders_an_explanatory_empty_state_not_a_table():
    out = _render({})
    assert "<table" not in out
    assert "No token usage recorded yet" in out


def test_disjoint_token_classes_get_their_own_columns():
    """in / cached / cache_write are disjoint as the providers report them. Folding them
    into a single Input figure while ALSO showing Cached as a peer column invited the
    reader to double-count the cached tokens against the input total."""
    out = _render({"claude:sonnet": {"in": 100, "cached": 40, "cache_write": 7, "out": 20}})
    cells = _cells(out)
    assert "100" in cells and "40" in cells and "7" in cells and "20" in cells, cells
    # the header must name all four, so no column is a hidden component of another
    for head in ("Input", "Cached", "Cache write", "Output"):
        assert head in out, f"missing column header {head!r}"


def test_cache_columns_are_hidden_when_no_provider_reports_them():
    # most providers never report cache classes; the columns would be dead weight
    out = _render({"openai:gpt-4o": {"in": 10, "out": 5}})
    assert "Cache write" not in out and "Cached" not in out
    assert "Input" in out and "Output" in out


def test_cost_uses_the_same_helper_as_the_topbar_pill():
    out = _render({"openai:gpt-4o": {"in": 1_000_000, "out": 1_000_000}},
                  {"gpt-4o": {"in": 2.5, "out": 10.0}})
    assert "$12.50" in out, out


def test_unpriced_models_are_named_and_the_total_says_it_is_partial():
    """A bare '+' suffix on the total was the only hint that some models were unpriced,
    and nothing on screen defined it. The shortfall has to be legible: how many models,
    and which ones."""
    out = _render({"openai:gpt-4o": {"in": 1_000_000, "out": 0},
                   "ollama:llama3": {"in": 500, "out": 100}},
                  {"gpt-4o": {"in": 2.5, "out": 10.0}})
    assert "not priced" in out
    assert "llama3" in out
    assert "1 of 2 model" in out, out
    assert "partial" in out.lower()


def test_no_partial_warning_when_every_model_is_priced():
    out = _render({"openai:gpt-4o": {"in": 1000, "out": 100}},
                  {"gpt-4o": {"in": 2.5, "out": 10.0}})
    assert "partial" not in out.lower() and "not priced" not in out


def test_rows_are_ordered_by_cost_so_the_expensive_model_is_first():
    out = _render({"openai:cheap": {"in": 1000, "out": 0},
                   "openai:dear": {"in": 1_000_000, "out": 0}},
                  {"cheap": {"in": 0.1, "out": 0}, "dear": {"in": 50.0, "out": 0}})
    assert out.index("dear") < out.index("cheap")


def test_a_model_name_containing_colons_is_shown_whole():
    """Fields are 'provider:model'; the model half carries colons of its own (ollama tags,
    openrouter ids), so only the FIRST colon separates provider from model."""
    out = _render({"ollama:qwen2.5:7b": {"in": 5, "out": 1}})
    cells = _cells(out)
    assert "ollama" in cells and "qwen2.5:7b" in cells, cells


def test_provider_and_model_are_escaped():
    """These strings originate in provider/model ids that reach Redis from config and
    model listings — they must not be able to inject markup into the Analytics pane."""
    out = _render({"<img src=x onerror=alert(1)>:<b>m</b>": {"in": 1, "out": 1}})
    assert "<img" not in out and "<b>m</b>" not in out
    assert "&lt;img" in out


def _foot_cells(html_str: str) -> list[str]:
    """The totals row's cells, by position. Slicing raw markup from '<tfoot' to the end of
    the document meant `"7" in foot` matched style="font-weight:700" — the assertion
    passed with every numeric cell blanked."""
    m = re.search(r"<tfoot.*?</tfoot>", html_str, re.S)
    assert m, "no totals row rendered"
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.S)]


def test_totals_row_sums_every_token_column():
    out = _render({"claude:a": {"in": 10, "cached": 1, "cache_write": 2, "out": 3},
                   "claude:b": {"in": 20, "cached": 4, "cache_write": 5, "out": 6}})
    cells = _foot_cells(out)
    assert cells == ["Total", "30", "5", "7", "9", "$0.00"], cells


def test_each_row_shows_its_own_cache_write_count():
    """The totals row alone cannot catch a per-row cell hardcoded to 0 — both had to be
    broken together before any assertion noticed."""
    out = _render({"claude:a": {"in": 10, "cached": 1, "cache_write": 2, "out": 3}})
    cells = _cells(out)
    assert "2" in cells, f"the row's cache-write count is missing: {cells}"


def test_cache_write_tokens_are_priced():
    """Cache creation is billed at ~1.25x input and is the most expensive token class in
    the table. Nothing anywhere exercised its pricing term: deleting it from _tokenCost
    left both JS test files green."""
    out = _render({"claude:s": {"in": 0, "cached": 0, "cache_write": 1_000_000, "out": 0}},
                  {"s": {"in": 3.0, "cache_write": 3.75, "out": 0}})
    assert "$3.75" in out, f"cache-write tokens are not priced: {_cells(out)}"


def test_cache_write_falls_back_to_1_25x_input_when_unpriced():
    """With no explicit cache_write rate the code bills 1.25x input — 1M x $4.00 x 1.25."""
    out = _render({"claude:s": {"in": 0, "cache_write": 1_000_000, "out": 0}},
                  {"s": {"in": 4.0, "out": 0}})
    assert "$5.00" in out, f"the 1.25x fallback is not applied: {_cells(out)}"


def test_zero_cost_renders_as_a_dollar_amount_not_an_empty_or_dash():
    # an all-free (local) setup must read as "$0.00", never as an ambiguous dash
    out = _render({"ollama:llama3": {"in": 500, "out": 100}}, {"llama3": {"in": 0, "out": 0}})
    # the ROW, not the totals line: the total prints $0.00 regardless, so checking the
    # whole document passed even when a free model's row read "not priced"
    assert "$0.00" in _cells(out), f"the free model's row does not show $0.00: {_cells(out)}"
    assert "not priced" not in out
