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
