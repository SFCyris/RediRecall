# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mermaid flowchart label-escaping transform, run as real JavaScript.

Models put (), ", %, and other specials inside an UNQUOTED flowchart node label —
``E[Brain "draws" a line]`` or ``D[Light bends (down)]`` — which mermaid's parser
rejects, blanking the whole diagram. ``_mermaidSafeLabels`` wraps each rectangle
label's content in quotes (making the specials safe) and folds inner ASCII quotes into
curly ones so they can't close the wrapper, leaving other diagram types, already-quoted
labels, and compound shapes alone.

Extracted from ``index.html`` and run under node — pure string work, no browser. The
actual mermaid parse/render of the transformed source is exercised manually (and the
transform is deterministic, so the string output fully characterises it).
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _fn() -> str:
    html = _INDEX.read_text(encoding="utf-8")
    s = html.index("function _mermaidSafeLabels(")
    return html[s:html.index("\n}", s) + 2]


def _transform(src: str) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _fn() + "\nconsole.log(JSON.stringify(_mermaidSafeLabels(" + json.dumps(src) + ")));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_unquoted_label_with_quotes_is_wrapped_and_inner_quotes_curled():
    out = _transform('graph TD\n D --> E[Brain "draws" a straight line back]')
    assert 'E["Brain “draws” a straight line back"]' in out
    assert '"draws"' not in out   # the raw ASCII quotes that broke the parser are gone


def test_unquoted_label_with_parentheses_is_wrapped():
    out = _transform('graph TD\n C --> D[Light Bends (down) toward your Eye]')
    assert 'D["Light Bends (down) toward your Eye"]' in out


def test_plain_label_is_quoted_harmlessly():
    assert _transform("graph TD\n A[Sunlight hits a Ship]") == \
        'graph TD\n A["Sunlight hits a Ship"]'


def test_already_quoted_label_is_left_alone():
    src = 'graph LR\n X["already quoted (safe)"] --> Y[plain]'
    out = _transform(src)
    assert 'X["already quoted (safe)"]' in out   # not double-wrapped
    assert 'Y["plain"]' in out


def test_cylinder_shape_is_preserved():
    # a [(cylinder)] label must NOT be turned into a rectangle
    assert _transform("graph LR\n A[(Database)] --> B[x]") == \
        'graph LR\n A[(Database)] --> B["x"]'


def test_quoted_label_with_literal_brackets_is_not_corrupted():
    # a correctly-quoted label may contain [ ] (arr[0], intervals) and renders fine today;
    # the transform must NOT re-split it on those brackets (that broke working diagrams)
    for lbl in ('A["arr[0] value"]', 'A["range [0,1]"]', 'A["item a] b"]'):
        out = _transform("graph LR\n " + lbl + " --> B[x]")
        assert lbl in out, f"a quoted-bracket label was corrupted: {out!r}"


def test_quoted_subgraph_title_with_brackets_is_left_alone():
    src = 'flowchart TD\n subgraph "My [x] title"\n  A[go] --> B[stop]\n end'
    assert 'subgraph "My [x] title"' in _transform(src)


@pytest.mark.parametrize("shape", ["A[/Read input/]", "A[\\wide bottom\\]", "A[/top\\]"])
def test_parallelogram_and_trapezoid_shapes_are_preserved(shape):
    # [/.../] etc are distinct shapes; wrapping them would turn them into a mislabeled
    # rectangle (the / \ delimiters becoming literal text)
    assert shape in _transform("graph LR\n " + shape + " --> B[x]")


def test_flowchart_behind_a_leading_comment_is_still_transformed():
    out = _transform("%% a comment\ngraph TD\n B[Light bends (down)]")
    assert 'B["Light bends (down)"]' in out


def test_digit_content_is_not_corrupted_by_the_placeholder():
    # the internal protect/restore must not collide with digit runs in real content
    out = _transform("graph TD\n A[step 3 of 5] --> B[phase 7 done]")
    assert 'A["step 3 of 5"]' in out and 'B["phase 7 done"]' in out


def test_less_than_in_a_quoted_label_becomes_the_lt_entity():
    # a literal '<' in a quoted label renders as "&lt;" under htmlLabels:false; mermaid's
    # #lt; entity renders a real '<' inside a quoted label (a real report: "No (< 50m)")
    out = _transform('graph TD\n B -- "No (< 50m)" --> C[x]')
    assert '"No (#lt; 50m)"' in out and '(< 50m)' not in out


def test_pre_escaped_lt_entity_is_also_rewritten():
    assert '#lt;' in _transform('graph TD\n A -- "x &lt; y" --> B[z]')


def test_ampersand_and_gt_outside_lt_are_left_intact():
    # only '<' / '&lt;' are rewritten — a bare '&' (R&D) and '>' must survive
    out = _transform('graph TD\n A -- "R&D < 5" --> B[a > b]')
    assert 'R&D' in out and 'a > b' in out and '#lt;' in out


def test_bidirectional_arrow_is_not_touched():
    # '<' in an edge arrow (never quoted) must NOT be rewritten
    assert _transform("graph LR\n A <--> B") == "graph LR\n A <--> B"


def test_less_than_in_a_node_label_becomes_the_fullwidth_glyph():
    # #lt; does NOT decode inside a node label (mermaid quirk), so a node-shape label uses
    # the fullwidth look-alike '＜' (renders literally) rather than #lt; (which would show &lt;)
    assert '["speed ＜ 50"]' in _transform("graph TD\n A[speed < 50] --> B[ok]")
    assert '["speed ＜ 50"]' in _transform('graph TD\n A["speed < 50"] --> B[ok]')
    assert '{"a ＜ b"}' in _transform("graph TD\n A{\"a < b\"} --> B[ok]")


def test_node_and_edge_less_than_use_different_glyphs_in_one_diagram():
    # edge label -> #lt; (real '<'); node label -> '＜' (fullwidth), in the same source
    out = _transform('graph TD\n B -- "far (< 9)" --> C[near < 9]')
    assert '"far (#lt; 9)"' in out          # edge label
    assert '["near ＜ 9"]' in out            # node label


@pytest.mark.parametrize("src", [
    'sequenceDiagram\n Alice->>John: Hello [not a label] "quote"',
    'classDiagram\n class Foo["bar"]',
    'gantt\n title A [section] "x"',
])
def test_non_flowchart_diagrams_are_untouched(src):
    assert _transform(src) == src


def test_mermaid_lane_calls_the_transform_before_render():
    """Guards the wiring, not just the helper: the mermaid lane must run the source
    through _mermaidSafeLabels before handing it to mermaid.render (paired with a
    mutations.json entry that removes the call and has been shown to break this)."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("mermaid:{")
    window = html[i:html.index("mermaid.render", i)]
    assert "_mermaidSafeLabels(src)" in window, \
        "the mermaid lane no longer sanitises labels before rendering"
