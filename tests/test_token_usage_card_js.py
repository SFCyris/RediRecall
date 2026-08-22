# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Analytics "Token Usage" card, run as real JavaScript.

The card is the only surface that answers "how many tokens have I actually burned"
across every session. It reads GET /api/usage, whose fields are disjoint token
classes (fresh input / cache read / cache write / output), so the column semantics
are the thing worth pinning down.

The card reports tokens and nothing else. It used to estimate a dollar cost from a
table of hand-written rates that no provider supplied and nothing revalidated —
prices change without notice, so the figure was as likely to be wrong as right.

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
    """The card renderer plus escHtml — the only thing standing between a
    provider-supplied model name and the Analytics DOM."""
    html = _INDEX.read_text(encoding="utf-8")
    return "\n".join(extract_js_function(html, n) for n in
                     ("escHtml", "_tokenUsageCardHTML"))


def _render(usage) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = ("const S={config:{}};\n" + _fns()
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
    for head in ("Input", "Cached", "Cache write", "Output"):
        assert head in out, f"missing column header {head!r}"


def test_cache_columns_are_hidden_when_no_provider_reports_them():
    # most providers never report cache classes; the columns would be dead weight
    out = _render({"openai:gpt-4o": {"in": 10, "out": 5}})
    assert "Cache write" not in out and "Cached" not in out
    assert "Input" in out and "Output" in out


def test_rows_are_ordered_by_total_tokens_so_the_heaviest_model_is_first():
    """Cost used to decide the order. Total volume is the ordering the card can still
    stand behind — it is measured, not inferred from a rate."""
    out = _render({"openai:light": {"in": 1000, "out": 0},
                   "openai:heavy": {"in": 1_000_000, "out": 0}})
    assert out.index("heavy") < out.index("light")


def test_cache_classes_count_towards_the_row_ordering():
    """A model whose volume is mostly cache reads still burned those tokens."""
    out = _render({"claude:mostly-cache": {"in": 10, "cached": 900_000, "out": 10},
                   "claude:plain": {"in": 5000, "out": 5000}})
    assert out.index("mostly-cache") < out.index("plain")


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
    assert cells == ["Total", "30", "5", "7", "9"], cells


def test_each_row_shows_its_own_cache_write_count():
    """The totals row alone cannot catch a per-row cell hardcoded to 0 — both had to be
    broken together before any assertion noticed."""
    out = _render({"claude:a": {"in": 10, "cached": 1, "cache_write": 2, "out": 3}})
    assert "2" in _cells(out), f"the row's cache-write count is missing: {_cells(out)}"


def test_the_card_quotes_no_money_at_all():
    """Guard against the estimate returning: no currency, no Cost column, and no claim
    that a total is partial for want of a rate."""
    out = _render({"openai:gpt-4o": {"in": 1_000_000, "cached": 5, "cache_write": 5,
                                     "out": 1_000_000}})
    assert "$" not in out, out
    assert "Cost" not in out
    assert "not priced" not in out and "partial" not in out.lower()


def test_the_card_says_where_the_rate_question_belongs():
    """Removing the figure without saying why just looks like a missing feature."""
    out = _render({"openai:gpt-4o": {"in": 10, "out": 5}})
    assert "your own bill" in out.lower(), out
