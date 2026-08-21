# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a JavaScript snippet under node via a temp FILE, not `node -e`.

`node -e <src>` passes the whole program as one argv element. Linux caps a single
argument at MAX_ARG_STRLEN (128 KB) regardless of ARG_MAX, so the larger snippets
here (the whole renderMarkdown pipeline, or a ReDoS input) raise
`OSError: [Errno 7] Argument list too long` on CI while passing on macOS, whose
limit is far higher. Writing the program to a file and running `node <file>`
removes the limit and behaves identically everywhere. Fixtures are required by
absolute path, so the temp file's location does not matter.
"""
import os
import subprocess
import tempfile


def run_node(js: str, timeout: int = 60) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(js)
        path = tf.name
    try:
        return subprocess.run(["node", path], capture_output=True,
                              text=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def extract_js_function(source: str, name: str) -> str:
    """Return exactly one function's source, matched by brace balance.

    Slicing from `function name(` to the next line-initial `}` is wrong for a one-liner:
    `_fmtCost` is a single line, so its slice ran on to the next function's closing brace
    and swallowed the whole of `_tokenUsageCardHTML` — which the test payload then defined
    twice. `escHtml` likewise dragged in five unrelated helpers. Neither broke a test, but
    the extraction was not doing what it read as, and a genuine second definition would
    silently shadow the first.

    Braces inside strings, template literals, regex literals and comments are skipped, so
    the count reflects real block structure.
    """
    start = source.index(f"function {name}(")
    # Keep a leading `async`: slicing from `function` alone drops it, and the extracted
    # body's `await` then fails to parse as "await is only valid in async functions".
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    i = source.index("{", start)
    depth, j, n = 0, i, len(source)
    while j < n:
        c = source[j]
        if c in "\"'`":                      # string / template literal
            quote, j = c, j + 1
            while j < n and source[j] != quote:
                j += 2 if source[j] == "\\" else 1
            j += 1
            continue
        if c == "/" and j + 1 < n:
            if source[j + 1] == "/":          # line comment
                j = source.find("\n", j)
                if j < 0:
                    break
                continue
            if source[j + 1] == "*":          # block comment
                j = source.find("*/", j) + 2
                continue
            # regex literal: a '/' in expression position, i.e. after an operator or '('
            k = j - 1
            while k >= 0 and source[k] in " \t":
                k -= 1
            if k >= 0 and source[k] in "=(,:[!&|?{};+-*%<>~^":
                j += 1
                while j < n and source[j] != "/":
                    j += 2 if source[j] == "\\" else 1
                j += 1
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
        j += 1
    raise ValueError(f"unbalanced braces extracting {name}()")
