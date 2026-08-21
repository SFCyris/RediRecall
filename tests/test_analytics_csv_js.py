# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Analytics CSV export, run as real JavaScript.

This is the file a user reconciles against a provider bill, and it had no test at all. It
also re-implements the provider/model split and calls ``_tokenCost`` a second time, so it
can drift away from the on-screen table silently — the two are asserted against each other
here rather than each being checked in isolation.

Extracted from index.html and run under node; the DOM and fetch are stubbed so the CSV
text itself is what gets inspected.
"""
import csv as _csv
import io
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _export(usage, pricing=None, metrics=None, instances=None) -> list[list[str]]:
    """Run exportAnalyticsCsv against stubbed state and return the parsed CSV rows."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    fns = "\n".join(extract_js_function(html, n)
                    for n in ("_tokenCost", "exportAnalyticsCsv"))
    metrics = metrics or {"hits": 3, "misses": 1, "latencies": [0.2, 0.4]}
    js = f"""
const S={{config:{{pricing:{json.dumps(pricing or {})}}},
          chatMetrics:{json.dumps(metrics)},
          ragInstances:{json.dumps(instances or [])}}};
// Capture the Blob text instead of downloading it.
let CAPTURED=null;
class Blob {{ constructor(parts){{ CAPTURED=parts.join(''); }} }}
const URL={{createObjectURL:()=>'blob:x', revokeObjectURL:()=>{{}}}};
const document={{createElement:()=>({{set href(v){{}}, set download(v){{}}, click(){{}}}})}};
const USAGE={json.dumps(usage)};
const fetch=async()=>({{json:async()=>USAGE}});
{fns}
exportAnalyticsCsv().then(()=>console.log(JSON.stringify(CAPTURED)));
"""
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    text = json.loads(r.stdout.strip().splitlines()[-1])
    assert text, "no CSV was produced"
    return list(_csv.reader(io.StringIO(text)))


def _find(rows, first_cell):
    return next((r for r in rows if r and r[0] == first_cell), None)


def test_headline_metrics_are_exported():
    rows = _export({})
    assert rows[0] == ["Metric", "Value"]
    assert _find(rows, "Total Queries")[1] == "4"
    assert _find(rows, "Cache Hits")[1] == "3"
    assert _find(rows, "Hit Rate %")[1] == "75"


def test_token_rows_are_included_with_a_header():
    """The export shipped without any token or cost data — the one table worth putting in
    a spreadsheet was the one you could not get out of the app."""
    rows = _export({"claude:sonnet": {"in": 10, "cached": 4, "cache_write": 7, "out": 3}})
    hdr = _find(rows, "Provider")
    assert hdr == ["Provider", "Model", "Input", "Cached", "Cache write", "Output", "Cost USD"]
    row = _find(rows, "claude")
    assert row[:6] == ["claude", "sonnet", "10", "4", "7", "3"], row


def test_cost_matches_the_on_screen_table_for_the_same_input():
    """The CSV calls _tokenCost separately from the card. Pin them to each other so the
    exported number cannot drift away from the one the user was shown."""
    usage = {"claude:s": {"in": 1_000_000, "cached": 0, "cache_write": 1_000_000, "out": 0}}
    pricing = {"s": {"in": 3.0, "cache_write": 3.75, "out": 0}}
    csv_cost = float(_find(_export(usage, pricing), "claude")[6])

    # the same figure as rendered by the card
    html = _INDEX.read_text(encoding="utf-8")
    fns = "\n".join(extract_js_function(html, n) for n in
                    ("_tokenCost", "_fmtCost", "escHtml", "_tokenUsageCardHTML"))
    js = (f"const S={{config:{{pricing:{json.dumps(pricing)}}}}};\n" + fns +
          f"\nconsole.log(JSON.stringify(_tokenUsageCardHTML({json.dumps(usage)})));\n")
    r = run_node(js, timeout=60)
    assert r.returncode == 0, r.stderr[:800]
    card = json.loads(r.stdout.strip().splitlines()[-1])

    assert round(csv_cost, 2) == 6.75, csv_cost          # 3.00 + 3.75
    assert f"${csv_cost:.2f}" in card, (
        f"the CSV says {csv_cost:.2f} but the card does not show it")


def test_an_unpriced_model_says_so_rather_than_exporting_a_zero():
    """A silent 0.0000 in a billing spreadsheet is worse than a gap — it reads as 'this
    model cost nothing' when the truth is 'nobody priced it'."""
    row = _find(_export({"ollama:llama3": {"in": 500, "out": 100}}), "ollama")
    assert row[6] == "not priced", row


def test_a_model_name_containing_a_comma_does_not_shift_the_columns():
    """Every field is quoted because model ids and instance names can contain commas —
    without it the row silently gains a column and every later value is off by one."""
    rows = _export({"openrouter:vendor,model:beta": {"in": 5, "out": 1}})
    row = _find(rows, "openrouter")
    assert row[1] == "vendor,model:beta", row
    assert len(row) == 7, f"the comma split the row into {len(row)} columns: {row}"


def test_a_model_name_containing_a_quote_is_escaped():
    rows = _export({'openai:say"hi"': {"in": 1, "out": 1}})
    assert _find(rows, "openai")[1] == 'say"hi"'


def test_rag_instance_chunk_counts_are_exported():
    rows = _export({}, instances=[{"name": "docs", "chunks": 42}])
    assert _find(rows, "RAG: docs chunks")[1] == "42"


def test_no_token_header_when_the_tally_is_empty():
    """An empty section header in a spreadsheet reads as missing data."""
    rows = _export({})
    assert _find(rows, "Provider") is None
