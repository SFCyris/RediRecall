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
import time

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "redirecall" / "index.html"


def _fn() -> str:
    return extract_js_function(_INDEX.read_text(encoding="utf-8"), "_mermaidSafeLabels")


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
    # a [(cylinder)] label must NOT be turned into a rectangle; its text is quoted in
    # place so specials inside it are safe too
    assert _transform("graph LR\n A[(Database)] --> B[x]") == \
        'graph LR\n A[("Database")] --> B["x"]'


def test_quoted_compound_shapes_are_not_mangled():
    # REGRESSION: the simple ["x"] / ("x") passes used to match the INNER delimiter pair of
    # a compound shape, leaving a bare [<placeholder>] that the rectangle pass then re-wrapped —
    # emitting A["["Sub"]"] and blanking a diagram that mermaid parsed perfectly well before.
    for lbl in ('A[["Sub (x)"]]', 'A[("Redis (db)")]'):
        out = _transform("graph LR\n " + lbl + " --> B[x]")
        assert lbl in out, f"a valid quoted compound shape was corrupted: {out!r}"


def test_stadium_is_not_demoted_to_a_round_node():
    # REGRESSION: brackets are ordinary characters to the round-shape matcher, so A([x])
    # could be rewritten as a ROUND node whose label literally read "[x]" — a silent shape
    # demotion that leaks the delimiters into the visible text.
    assert _transform("graph LR\n A([Answer]) --> B[x]") == \
        'graph LR\n A(["Answer"]) --> B["x"]'


def test_quoted_label_with_literal_brackets_is_not_corrupted():
    # a correctly-quoted label may contain [ ] (arr[0], intervals) and renders fine today;
    # the transform must NOT re-split it on those brackets (that broke working diagrams)
    for lbl in ('A["arr[0] value"]', 'A["range [0,1]"]', 'A["item a] b"]'):
        out = _transform("graph LR\n " + lbl + " --> B[x]")
        assert lbl in out, f"a quoted-bracket label was corrupted: {out!r}"


def test_quoted_subgraph_title_with_brackets_is_left_alone():
    src = 'flowchart TD\n subgraph "My [x] title"\n  A[go] --> B[stop]\n end'
    assert 'subgraph "My [x] title"' in _transform(src)


@pytest.mark.parametrize("shape,quoted", [
    ("A[/Read input/]",    'A[/"Read input"/]'),
    ("A[\\wide bottom\\]", 'A[\\"wide bottom"\\]'),
    ("A[/top\\]",          'A[/"top"\\]'),
])
def test_parallelogram_and_trapezoid_shapes_are_preserved(shape, quoted):
    # [/.../] etc are distinct shapes; the delimiters must survive verbatim (turning them
    # into label text would silently demote the node to a mislabeled rectangle), while the
    # text between them gets quoted so specials inside it can't break the parse
    assert quoted in _transform("graph LR\n " + shape + " --> B[x]")


@pytest.mark.parametrize("shape,quoted", [
    ("A[ /Read input/ ]",    'A[/"Read input"/]'),
    ("A[ \\wide bottom\\ ]", 'A[\\"wide bottom"\\]'),
    ("A[ /top\\ ]",          'A[/"top"\\]'),
    ("A[ /In (raw)/ ]",      'A[/"In (raw)"/]'),
    ("A[( cyl (x) )]",       'A[(" cyl (x) ")]'),
    ("A[[ sub (x) ]]",       'A[[" sub (x) "]]'),
])
def test_a_padded_compound_shape_is_tightened_not_demoted(shape, quoted):
    """A space between the outer bracket and the inner delimiter is a shape mermaid accepts
    only while the label is plain: `A[ /Read input/ ]` parses, `A[ /In (raw)/ ]` does not,
    and neither does the padded-but-quoted `A[ /"In (raw)"/ ]` (all three checked against
    11.12). The one form that renders is the tight quoted one, so the padding is dropped on
    the way out.

    What must NOT happen is the delimiters becoming label text — that silently demotes the
    node to a mislabeled rectangle, which is what this asserts against.
    """
    out = _transform("graph LR\n " + shape + " --> B[x]")
    assert quoted in out, "a padded compound shape was not rescued into its tight form"
    assert '["' + shape.split("[", 1)[1] not in out, "the shape was demoted to a rectangle"


@pytest.mark.parametrize("src,expect", [
    # Two levels of nesting — the pattern used to allow exactly one and fall through here.
    ("graph LR\n A(a (b (c)) d) --> B", 'A("a (b (c)) d")'),
    ("graph LR\n A{{a (b (c)) d}} --> B", 'A{{"a (b (c)) d"}}'),
    # Three, and the arrow after it still must not be swallowed.
    ("graph LR\n A(w (x (y (z)))) --> B[q]", 'A("w (x (y (z)))")'),
])
def test_deeply_nested_parens_in_a_shape_label_are_rescued(src, expect):
    out = _transform(src)
    assert expect in out
    assert "--> B" in out, "the non-greedy match spanned the arrow"


# ── Inline `-- text -->` edge labels ──────────────────────────────────────────
# Measured against mermaid 11.12: of ( ) [ ] { } | < > & % ; : , ' = # - the only character
# that makes an unquoted inline label a parse error is the double quote, and a raw '<'
# parses but paints the literal text "&lt;". So exactly two repairs apply, and a label
# needing neither must come through untouched.

@pytest.mark.parametrize("src,expect", [
    ('graph LR\n A -- say "hi" --> B',   'A -- "say \u201chi\u201d" --> B'),
    ('graph LR\n A == cost "x" ==> B',   'A == "cost \u201cx\u201d" ==> B'),
    ('graph LR\n A -. maybe "y" .-> B',  'A -. "maybe \u201cy\u201d" .-> B'),
    ('graph LR\n A-- tight "q" -->B',    'A-- "tight \u201cq\u201d" -->B'),
])
def test_a_quote_inside_an_inline_edge_label_is_wrapped(src, expect):
    """An unquoted inline label containing a double quote is a hard parse error, so the
    whole label is quoted and its own quotes folded to curly ones."""
    assert expect in _transform(src)


@pytest.mark.parametrize("src,expect", [
    ("graph LR\n A -- less < 50m --> B",     "A -- less #lt; 50m --> B"),
    ("graph LR\n A -- less &lt; 50m --> B",  "A -- less #lt; 50m --> B"),
])
def test_a_raw_lt_in_an_inline_edge_label_becomes_the_entity(src, expect):
    """'<' parses here but paints as the literal string "&lt;". '#lt;' paints a real '<' in
    an inline label exactly as it does in a pipe label (both verified by rendering)."""
    assert expect in _transform(src)


@pytest.mark.parametrize("src", [
    "graph LR\n A --> B --> C",             # "> B " has the shape of a label; it is not one
    "graph LR\n A -- a > b --> B",          # '>' inside a label is legal and renders as '>'
    "graph LR\n A -- plain --> B",
    "graph LR\n A --- B",
    "graph LR\n A -- a, b; c: d --> B",
    'graph TD\n A -- "already quoted" --> B',
])
def test_an_inline_edge_label_needing_no_repair_is_untouched(src):
    assert _transform(src) == src


@pytest.mark.parametrize("src,expect", [
    # A rectangle label may contain the very tokens the inline-edge pass anchors on.
    ("graph TD\n A[Phase 1 -- setup] --> B", 'A["Phase 1 -- setup"]'),
    ("graph TD\n A[x == y] --> B",           'A["x == y"]'),
    ("graph TD\n A[a -. b] --> C",           'A["a -. b"]'),
    ("graph TD\n A[p--q] --> B --> C",       'A["p--q"]'),
])
def test_an_unquoted_rectangle_label_containing_a_link_token_survives(src, expect):
    """The rectangle pass is the only one that used to return its result into the working
    string instead of holding it, so the inline-edge pass — which runs last, on the premise
    that every shape is already held — could still see inside it and split the label through
    its own middle: `A[Phase 1 -- setup]` came out as `A["Phase 1 -- "setup”]"`, an
    unbalanced quote that blanks the diagram. These four rendered before the inline-edge
    pass existed and must keep rendering.
    """
    out = _transform(src)
    assert expect in out, out
    assert '"' not in out.split(expect)[1].split("\n")[0], f"a stray quote escaped: {out}"


@pytest.mark.parametrize("src,expect", [
    # `cost` is not a node id — it is a word inside a label that happens to precede a paren.
    ("graph TD\n A[cost(usd)] --> B",     'A["cost(usd)"]'),
    ("graph TD\n A[f(x)] --> B",          'A["f(x)"]'),
    ("graph TD\n A[sin(x) curve] --> B",  'A["sin(x) curve"]'),
    ("graph TD\n A[getUser(id)] --> B",   'A["getUser(id)"]'),
    ("graph TD\n A[config{a:1}] --> B",   'A["config{a:1}"]'),
])
def test_a_word_inside_a_label_is_not_read_as_a_node_id(src, expect):
    """The id-anchored shape passes take any word not preceded by a word character. That
    admitted '[', so the `cost` of `A[cost(usd)]` read as a node id and `(usd)` as a round
    shape — the label was rewritten to `A["cost("usd")"]` and stopped parsing. Function-call
    shaped labels are ordinary model output for engineering diagrams.
    """
    assert expect in _transform(src)


def test_a_node_label_containing_a_dash_pair_is_not_read_as_an_edge():
    """The inline-edge pass anchors on '--', which can occur inside a node label. It runs
    after every node shape has been held, so `A["p--q"] --> B` must survive intact — split
    through its own label it would emit `A["p-- "q"] " -->B`."""
    assert _transform('graph LR\n A["p--q"] --> B') == 'graph LR\n A["p--q"] --> B'


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


@pytest.mark.parametrize("arrow", ["-->", "---", "-.->", "==>", "--x", "--o"])
def test_unquoted_pipe_label_with_specials_is_wrapped_for_every_arrow(arrow):
    # an unquoted edge label -->|yes (ok)| blanks the diagram exactly as an unquoted node
    # label did; the wrap is anchored on the arrow tail, so every arrow form must be covered
    out = _transform(f"graph TD\n A {arrow}|yes (ok)| B[x]")
    assert '|"yes (ok)"|' in out, out


@pytest.mark.parametrize("src,want", [
    # No space around the pipe, and the character before it is a plain letter that also
    # happens to be an arrow-tail character: the 'o' of "Ratio", the 'x' of "Max". Anchoring
    # the wrap on a single tail character matched both and mangled the labels — spacing the
    # pipes out (a | b) hides the defect entirely, so these cases must stay tight.
    ("graph TD\n A[Ratio|Score] --> B[Auto|Manual]", 'graph TD\n A["Ratio|Score"] --> B["Auto|Manual"]'),
    ("graph TD\n A[Max|Min] --> B[Up|Down]",         'graph TD\n A["Max|Min"] --> B["Up|Down"]'),
    ("graph TD\n A[a | b] --> C[c | d]",             'graph TD\n A["a | b"] --> C["c | d"]'),
])
def test_pipes_inside_two_unquoted_node_labels_are_not_paired_into_an_edge_label(src, want):
    # REGRESSION: a bare /\|..\|/ sweep pairs the '|' of one node label with the '|' of the
    # NEXT one, swallowing the arrow between them and corrupting both labels
    assert _transform(src) == want


def test_a_node_label_pipe_does_not_swallow_a_real_edge_label():
    # the worst form of the mis-pairing: it destroys the node label AND the edge label
    assert _transform("graph TD\n A[x|y] -->|lbl| B") == 'graph TD\n A["x|y"] -->|"lbl"| B'


@pytest.mark.parametrize("inner", ["cost 5(usd)", "arr[0] hit", "a {b} c"])
def test_edge_label_delimiters_are_not_claimed_by_the_node_shape_passes(inner):
    """An edge label is arbitrary prose and routinely contains the delimiters the node-shape
    passes look for. Those passes must never reach inside one: a '5(usd)' claimed as a round
    node put quotes *inside* the pipe label and broke it."""
    out = _transform(f"graph TD\n A -->|{inner}| B")
    assert out == f'graph TD\n A -->|"{inner}"| B', out


def test_no_placeholder_sentinel_survives_into_the_output():
    """The protect/restore sentinel must never reach mermaid. It leaked two ways: a value
    pushed by a later pass could still contain an earlier placeholder (the single restore
    pass resumes scanning after the text it inserted), and a U+FFFF present in the SOURCE
    was read as a placeholder and swapped for another label's content."""
    for src in ("graph TD\n A -->|cost 5(usd)| B",
                "graph TD\n A[(db (x))] -->|read [0]| B[y]",
                "graph TD\n A[has ￿0￿ here]",
                'graph TD\n A["quoted"] --> B[lit ￿0￿ text]',
                # A node label whose text happens to contain an arrow-and-pipe run: the pipe
                # pass claims it and pushes, then the round-shape pass wraps the result and
                # pushes AGAIN — the only shape that actually nests one placeholder inside
                # another, and so the only one that catches a non-recursive restore.
                "graph TD\n A(cost --o|x| more) --> B"):
        out = _transform(src)
        assert "￿" not in out, f"sentinel leaked: {out!r}"
        assert "undefined" not in out, f"unresolved placeholder became 'undefined': {out!r}"


def test_pipe_label_less_than_uses_the_entity_not_the_fullwidth_glyph():
    # edge/pipe labels DO decode #lt; to a real '<', so they must not get the node fallback
    out = _transform("graph TD\n A -->|dist < 50m| B[Near]")
    assert '|"dist #lt; 50m"|' in out and '＜' not in out


def test_subgraph_title_uses_the_entity_like_other_edge_contexts():
    # a subgraph title decodes #lt; to a real '<' (verified against mermaid 11.12), so it
    # takes the entity rather than the fullwidth look-alike node labels need
    out = _transform('flowchart TD\n subgraph "S < T"\n A[go]\n end')
    assert 'subgraph "S #lt; T"' in out and '＜' not in out


@pytest.mark.parametrize("raw,quoted", [
    ("A[Val (x)]",     'A["Val (x)"]'),
    ("A(Val (x))",     'A("Val (x)")'),
    ("A([Val (x)])",   'A(["Val (x)"])'),
    ("A[[Val (x)]]",   'A[["Val (x)"]]'),
    ("A[(Val (x))]",   'A[("Val (x)")]'),
    ("A((Val (x)))",   'A(("Val (x)"))'),
    ("A>Val (x)]",     'A>"Val (x)"]'),
    ("A{Val (x)}",     'A{"Val (x)"}'),
    ("A{{Val (x)}}",   'A{{"Val (x)"}}'),
    ("A(((Val (x))))", 'A((("Val (x)")))'),
])
def test_every_shape_normalises_to_its_own_quoted_form(raw, quoted):
    """Each flowchart shape must end up quoted (so specials parse) while keeping its OWN
    delimiters — a shape rewritten with a different delimiter pair still renders, so the
    only thing that catches the demotion is asserting the delimiters verbatim."""
    assert quoted in _transform("graph TD\n " + raw + " --> Z[end]")


@pytest.mark.parametrize("src", [
    "graph TD\n A --> B",
    "graph TD\n A-->B",
    "graph LR\n A <--> B",
    "graph TD\n A -.-> B",
    "graph TD\n A ==> B",
    "graph TD\n A --x B",
    "graph TD\n A --o B",
])
def test_plain_arrows_are_never_mistaken_for_a_shape(src):
    # '-' must not count as a node-id character: with it, the asymmetric ID>x] pattern reads
    # the 'A--' of a plain A-->B edge as a node id and eats the arrow
    assert _transform(src) == src


@pytest.mark.parametrize("src,want", [
    # The arrow must be followed by a label ending in ']' on the SAME line, or the
    # asymmetric pattern can never fire and this defect is undetectable. Every parameter
    # of the test above lacked one, so re-adding '-' to the id class broke eight other
    # tests and left that one green.
    ("graph TD\n A --> B[x]",   'graph TD\n A --> B["x"]'),
    ("graph TD\n A-->B[x]",     'graph TD\n A-->B["x"]'),
    ("graph TD\n A ==> B[y]",   'graph TD\n A ==> B["y"]'),
    ("graph TD\n A -.-> B[z]",  'graph TD\n A -.-> B["z"]'),
    ("graph LR\n A <--> B[q]",  'graph LR\n A <--> B["q"]'),
])
def test_an_arrow_before_a_bracket_label_is_not_eaten_by_the_asymmetric_shape(src, want):
    assert _transform(src) == want


@pytest.mark.parametrize("stmt", [
    # These MUST carry text the shape passes would otherwise claim. With `myCb()` alone the
    # test passed even with the directive hold deleted: the empty parens make the round-shape
    # pass decline, and the other statements have no parens or braces at all — so nothing
    # was being protected and nothing could fail.
    "click A call myFn(1,2)",
    "click A call fn(a) then",
    'click A "https://e.com" "Tip (info)"',
    "style A fill:#f9f,stroke:#333",
    "classDef hi fill:#f9f",
    "linkStyle 0 stroke:#f00",
])
def test_directive_statements_keep_their_syntax_parens(stmt):
    # parens/braces in a click or style statement are syntax, not label text — the shape
    # passes would otherwise "fix" them into garbage
    assert stmt in _transform("graph TD\n A[x] --> B[y]\n " + stmt)


@pytest.mark.parametrize("directive", [
    '%%{init:{"theme":"dark"}}%%',
    '%%{init: {"theme":"dark"}}%%',
    # A MULTI-LINE directive whose continuation line carries a paren. This is the case the
    # %%{...}%% hold actually exists for: a single-line directive is already covered by the
    # %% comment hold, so with only single-line parameters the whole test stayed green with
    # that hold deleted. Without it this becomes  "g rgb("1,2,3")"  — broken JSON.
    '%%{init: {\n  "themeCSS": "g rgb(1,2,3)"\n}}%%',
    '%%{init: {\n  "flowchart": {"htmlLabels": false}\n}}%%',
])
def test_init_directive_survives_and_the_diagram_is_still_transformed(directive):
    out = _transform(directive + '\ngraph TD\n A[x (y)] --> B[z]')
    assert directive in out, f"the init directive was rewritten: {out!r}"
    assert 'A["x (y)"]' in out, "the diagram itself was not transformed"


def test_mermaid_lane_calls_the_transform_before_render():
    """Guards the wiring, not just the helper: the mermaid lane must run the source
    through _mermaidSafeLabels before handing it to mermaid.render (paired with a
    mutations.json entry that removes the call and has been shown to break this)."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("mermaid:{")
    window = html[i:html.index("mermaid.render", i)]
    assert "_mermaidSafeLabels(src)" in window, \
        "the mermaid lane no longer sanitises labels before rendering"


# ── the transform runs on model output, so its regexes must not be a hang ─────

def test_the_label_passes_do_not_backtrack_catastrophically():
    """Every pass here is a regex over text a model wrote, and two of them nest a
    quantified alternation: the paren shape allows three levels of balanced parens, and the
    inline-edge pass scans a non-greedy body between two link tokens. Both are the classic
    shape of a hang, and the inputs that trigger one — an unclosed paren run, a line of
    link tokens with no valid closer — are exactly what a truncated or malformed answer
    looks like. A single blown case freezes the tab, so this is a wall-clock assertion.
    """
    if not shutil.which("node"):
        pytest.skip("node not available")
    cases = {
        "deep unbalanced parens": "graph LR\n A(" + "(" * 40 + "x",
        "long balanced run":      "graph LR\n A(" + "a(" * 30 + "b" + ")" * 30 + ")",
        "dash storm, no closer":  "graph LR\n A " + "-- x " * 200,
        "quote storm in an edge": 'graph LR\n A -- ' + 'say "hi" ' * 200 + '--> B',
        "arrow chain":            "graph LR\n " + " --> ".join(f"N{i}" for i in range(400)),
        "unclosed brackets":      "graph LR\n A" + "[" * 500 + "x",
        "mixed link tokens":      "graph LR\n " + " ".join(["A", "--", "x", "==",
                                                            "y", ".-", "z"] * 150),
    }
    src = _INDEX.read_text(encoding="utf-8")
    js = (extract_js_function(src, "_mermaidSafeLabels")
          + "\nconst C=" + json.dumps(cases) + ";\nconst out={};\n"
          + "for(const [k,v] of Object.entries(C)){const t=Date.now();"
            "try{_mermaidSafeLabels(v);}catch(e){}out[k]=Date.now()-t;}\n"
            "console.log(JSON.stringify(out));\n")
    started = time.time()
    r = run_node(js, timeout=45)
    assert r.returncode == 0, r.stderr[:800]
    timings = json.loads(r.stdout.strip().splitlines()[-1])
    slow = {k: ms for k, ms in timings.items() if ms > 1000}
    assert not slow, f"a label pass backtracked catastrophically: {slow}"
    assert time.time() - started < 40, "the whole run was suspiciously slow"


# ── the timeline dialect: colons inside a period ─────────────────────────────
# mermaid's timeline grammar ends a period at a colon FOLLOWED BY WHITESPACE. A colon that
# is not — the one in a clock time — is a lexer error, so `2024-01-01 00:00 : Sunrise`
# could not be rendered at all, and the error it produced pointed at the `timeline` header
# rather than at the colon. Every rule asserted below was measured against mermaid 11.12.

def _timeline(src: str) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    fn = extract_js_function(_INDEX.read_text(encoding="utf-8"), "_timelineSafeColons")
    js = fn + "\nconsole.log(JSON.stringify(_timelineSafeColons(" + json.dumps(src) + ")));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_clock_time_in_a_timeline_period_is_escaped():
    """The reported case. `#58;` is mermaid's own entity for a colon and paints a real ':'
    in a timeline — checked by rendering, because the same entity paints literal garbage
    inside a flowchart node label."""
    out = _timeline("timeline\n2024-01-01 00:00 : Sunrise in New York\n"
                    "2024-01-01 06:00 : Sunrise in London")
    assert "2024-01-01 00#58;00 : Sunrise in New York" in out, out
    assert "2024-01-01 06#58;00 : Sunrise in London" in out, out


def test_the_period_separator_itself_is_never_escaped():
    """A colon followed by whitespace IS the separator. Escaping it would merge the period
    and the event into one unparseable label."""
    out = _timeline("timeline\n2024-01-01 : Sunrise")
    assert out == "timeline\n2024-01-01 : Sunrise"


def test_a_colon_after_the_separator_is_left_alone():
    """Only the period is constrained — `: Sunrise at 06:30` already parses and renders, so
    rewriting it would put '#58;' in front of a reader for no reason."""
    out = _timeline("timeline\n2024-01-01 00:00 : Sunrise at 06:30 : Sunset at 18:45")
    assert out == "timeline\n2024-01-01 00#58;00 : Sunrise at 06:30 : Sunset at 18:45"


def test_a_colon_in_a_title_is_left_alone_even_without_a_following_space():
    """`title` takes the whole rest of the line, so `title Sprint 12:00 review` already
    parses — and the title is rendered verbatim, which is the one place a stray '#58;'
    would be visible to the reader.

    The colon here is deliberately NOT followed by a space: with one, the separator rule
    would leave it alone anyway and this would prove nothing about the exemption.
    """
    out = _timeline("timeline\ntitle Sprint 12:00 review\n2024 : x")
    assert out == "timeline\ntitle Sprint 12:00 review\n2024 : x", out


@pytest.mark.parametrize("kw", ["acc_title", "acc_descr"])
def test_the_accessibility_lines_are_repaired_despite_looking_like_titles(kw):
    """They read like `title` and behave like a period: `acc_title Sprint 12:00 review` is
    a parse error, while `title Sprint 12:00 review` is not. Exempting them alongside
    `title` — the obvious grouping — leaves the diagram unrenderable."""
    out = _timeline(f"timeline\n{kw} Sprint 12:00 review\n2024 : x")
    assert f"{kw} Sprint 12#58;00 review" in out, out


def test_a_colon_in_a_section_label_is_escaped():
    """A section is NOT like a title: `section 12:00 Noon` is a parse error."""
    assert "section 12#58;00 Noon" in _timeline("timeline\nsection 12:00 Noon\n2024 : x")


def test_a_line_with_no_separator_has_every_colon_escaped():
    """With no colon-followed-by-whitespace there is no period/event split, so the whole
    line is the period and every colon in it is a lexer error."""
    assert _timeline("timeline\n2024-01-01 00:00") == "timeline\n2024-01-01 00#58;00"


def test_a_url_in_a_period_survives():
    assert "https#58;//example.com : Launched" in _timeline("timeline\nhttps://example.com : Launched")


def test_escaping_is_idempotent():
    """The lane runs on whatever the model wrote, which may already use the entity."""
    once = _timeline("timeline\n2024-01-01 00:00 : Sunrise")
    assert _timeline(once) == once


def test_the_timeline_header_is_never_rewritten():
    assert _timeline("timeline\n2024 : x").startswith("timeline\n")
