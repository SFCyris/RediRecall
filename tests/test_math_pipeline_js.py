# SPDX-License-Identifier: AGPL-3.0-or-later
"""Math-pipeline regression tests that EXECUTE renderMarkdown, not grep it.

These guard the B1/B3 fix (internal/OPEN-ISSUES-1.5.0.md): math is now tokenised by
marked itself (the rrMath extension in setupMarked), instead of being pre-extracted
from the raw text with a global regex. The three defects this closed only manifest
once the *real* marked runs, so the suite loads the exact vendored build
(tests/fixtures/marked.umd.js, marked 16.3.0 — the same version index.html loads from
the CDN) and runs the real renderMarkdown under node.

DOMPurify is a pass-through stub: every one of these corruptions is already present in
the HTML *before* the sanitiser, so a stub is enough and keeps the test off jsdom.

Each behaviour below is paired with an entry in tests/mutations.json that reintroduces
the old defect and has been shown to make the matching test go red — a source-level
grep would not have caught any of them (the old buggy regex, the sentinel leak, and
the alt-attribute span injection all leave the identifiers in place).
"""
import json
import pathlib
import shutil
import subprocess
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "redirecall" / "index.html"
_MARKED = pathlib.Path(__file__).resolve().parent / "fixtures" / "marked.umd.js"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _fn(html: str, header: str) -> str:
    """A function body: from its header to the first brace at column 0."""
    s = html.index(header)
    return html[s:html.index("\n}", s) + 2]


def _one(html: str, header: str) -> str:
    """A single-line declaration."""
    s = html.index(header)
    return html[s:html.index("\n", s)]


def _between(html: str, a: str, b: str, extra: int = 0) -> str:
    s = html.index(a)
    e = html.index(b, s)
    return html[s:e + extra]


def _assemble(html: str) -> str:
    """Everything renderMarkdown needs, in dependency order, for a node context."""
    viz = (_between(html, "let _vizP=null;", "\n")
           + "\n" + html[html.index("const _vizInst="):
                         html.index("\n", html.index("const _vizInst="))])
    return "\n".join([
        viz,
        'const CDN="";',
        "const _loadScript=()=>Promise.resolve();",
        "const _loadCss=()=>Promise.resolve();",
        _one(html, "function escHtml(s){"),
        _fn(html, "function _wrapLooseSvg(text){"),
        _fn(html, "function _wrapImagesInCards(html){"),
        _fn(html, "function _lane(kind){"),
        _between(html, "const RICH_LANES={", "\n};\n", 3),
        _one(html, "let _rrMathFinalize=true;"),
        _between(html, "(function setupMarked(){", "})();", 5),
        _fn(html, "function renderMarkdown(text,opts){"),
    ])


def _render(md, opts=None, timeout: int = 60) -> str:
    """Run the real renderMarkdown(md, opts) under node and return its HTML string."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    if not _MARKED.exists():
        pytest.skip("vendored marked fixture missing")
    stubs = (
        f"const marked = require({json.dumps(str(_MARKED))}).marked;\n"
        # pass-through: the B1/B3 corruptions are present before sanitisation
        "const DOMPurify = { sanitize:(x)=>String(x) };\n"
        "const window = { DOMPurify, katex:null };\n"
        "const document = { documentElement:{getAttribute:()=>null}, addEventListener:()=>{} };\n"
        "const Viz = { instance: async()=>({renderSVGElement(){return"
        "{outerHTML:'<svg></svg>',removeAttribute(){},style:{}}}}) };\n"
    )
    tail = (f"const __o = renderMarkdown({json.dumps(md)}, {json.dumps(opts if opts is not None else {'math': True})});\n"
            "process.stdout.write(String(__o));\n")
    js = stubs + _assemble(_html()) + "\n" + tail
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1600]}"
    return r.stdout


# ── B1: currency and math in the same paragraph ─────────────────────────────────
def test_issue_b1_currency_then_math_not_corrupted():
    """Old bug: '$20 if the error $e$' paired the currency $ with the math $, giving a
    span with data-tex='20 if the error' and a dangling literal e$. After the fix the
    currency stays plain text and only the real $e$ becomes a single math span."""
    out = _render("Refund $20 if the error $e$ exceeds tolerance.")
    assert 'data-tex="20 if the error"' not in out, out          # the exact old corruption
    assert 'data-tex="e"' in out, out                            # the real math rendered
    assert out.count('class="rr-math"') == 1, out                # exactly one span
    assert "$20" in out, out                                     # currency left literal
    assert "exceeds tolerance." in out and "e$" not in out, out  # no dangling delimiter


def test_issue_b1_currency_math_currency_all_correct():
    """A currency, a math span, and a second currency in one CJK-mixed sentence."""
    out = _render("The 价格 is $10 and 公式 $y=x$ holds.")
    assert 'data-tex="10 and 公式"' not in out, out
    assert 'data-tex="y=x"' in out, out
    assert "$10" in out and out.count('class="rr-math"') == 1, out


# ── B1 corollary: the tokeniser regex must not backtrack catastrophically ───────
def test_issue_b1_inline_regex_is_redos_safe():
    """The naive /(?:\\.|[^$\\n])*?[^\\s$\\n]/ form hangs for seconds on '$'+('a\\')*N with
    no closing $, because its two content alternatives both match a backslash. The
    shipped disjoint form must return promptly."""
    pathological = "$" + "a\\" * 40000 + " "
    start = time.time()
    try:
        _render(pathological, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("inline math regex catastrophically backtracked (ReDoS) on a "
                    "'$' + many 'a\\' pairs input with no close")
    assert time.time() - start < 18, "render was suspiciously slow — possible ReDoS"


# ── B3: sentinels / spans escaping into URLs and attributes ─────────────────────
def test_issue_b3_href_dollar_stays_literal_not_a_dead_link():
    """Old bug: $a^2$ inside a link URL was pre-extracted to a PUA sentinel that marked
    then encodeURI'd into %EE%80%80…0…%EE%80%81, a dead link. The $ must survive in the
    href and no sentinel may appear."""
    out = _render("[doc](https://ex.com/p/$a^2$/end)")
    assert "%EE%80%80" not in out, out
    assert "" not in out and "" not in out, out
    assert 'href="https://ex.com/p/$a' in out, out               # literal $ preserved
    assert "rr-math" not in out, out                             # URL text is not math


def test_issue_b3_image_alt_math_is_literal_not_a_span():
    """Old bug: $x^2$ in an image alt became an rr-math <span> that marked rendered
    straight INTO the alt="" attribute, truncating it to alt='<span class=' and leaking
    'span class='&gt; markup out of the card. The alt must hold the raw source."""
    out = _render("![$x^2$](http://x/y.png)")
    assert 'alt="$x^2$"' in out, out
    assert 'alt="<span' not in out, out
    assert "rr-math" not in out, out                             # no math span for an alt
    assert "🖼 $x^2$" in out, out                                 # card label is the raw source


def test_issue_b3_image_src_dollar_math_not_sentinel_encoded():
    """Old bug: $a^2$ in an image SRC path pre-extracted to a sentinel and marked
    encodeURI'd it to %EE%80%80… — a broken image URL."""
    out = _render("![diagram](http://x/$a^2$/img.png)")
    assert "%EE%80%80" not in out and "" not in out, out
    assert "$a" in out and "img.png" in out, out


# ── positive guards: correct-today behaviour that must keep working ─────────────
def test_currency_only_is_never_math():
    out = _render("It costs $5 and $10 total.")
    assert "rr-math" not in out, out
    assert "$5 and $10" in out, out


def test_shell_variable_in_code_stays_literal():
    out = _render("Set `$PATH` and `$HOME` in your shell.")
    assert "rr-math" not in out, out
    assert "$PATH" in out and "$HOME" in out, out


def test_real_inline_math_renders_as_span():
    out = _render("The identity $e^{i\\pi}+1=0$ is famous.")
    assert out.count('class="rr-math"') == 1, out
    assert 'data-tex="e^{i\\pi}+1=0"' in out, out


def test_display_math_is_a_display_span():
    out = _render("Euler: $$e^{i\\pi}+1=0$$ done.")
    assert 'class="rr-math rr-display"' in out and 'data-display="1"' in out, out


def test_streaming_mode_emits_raw_delimiters_not_spans():
    """opts.math falsy (mid-stream): a complete formula shows as raw $…$ text, so a
    half-typed one never flashes a broken span."""
    out = _render("Value $e$ mid stream.", opts={})
    assert "rr-math" not in out, out
    assert "$e$" in out, out
