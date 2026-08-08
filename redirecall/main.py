# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Sebastian Cyris
#
# RediRecall is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file,
# or <https://www.gnu.org/licenses/>, for details.
#
# Source: https://github.com/SFCyris/RediRecall
"""RediRecall — FastAPI backend (composition root).

The implementation was split out of this file into sibling modules for
maintainability; see each ``redirecall.<name>`` module. This root imports every
module (registering their routes on the shared ``app``), keeps the CLI entry
point, and re-exports every public name live via ``__getattr__`` so that
``redirecall.main.X`` — and test access/patching against the owning module —
continue to resolve exactly as before the split.
"""
import logging
import os

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from redirecall import __version__
from . import (
    constants, appcore, state, config, redis_store, embeddings, sessions, rag, cache, textutil, ingest, crawler, providers, hyde, rag_admin, ws, startup as _mod_startup, routes_settings, routes_media, routes_redis, routes_instances, routes_ingestion, routes_monitor, routes_misc, routes_chat, routes_sources, routes_frontend,
)
from .appcore import app

# Retained for import-surface parity with the pre-split module (both were imported
# at the top of the original main.py). Unused by the code itself.
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

# Every submodule, consulted in turn by __getattr__ below. Names are unique across
# modules, so order does not affect resolution. A module whose name collides with a
# public function (see above) is aliased _mod_<name> so the bare name resolves to
# the function, not the module.
_MODULES = [
    constants, appcore, state, config, redis_store, embeddings, sessions, rag, cache, textutil, ingest, crawler, providers, hyde, rag_admin, ws, _mod_startup, routes_settings, routes_media, routes_redis, routes_instances, routes_ingestion, routes_monitor, routes_misc, routes_chat, routes_sources, routes_frontend,
]


def __getattr__(name):
    for _m in _MODULES:
        try:
            return getattr(_m, name)
        except AttributeError:
            continue
    raise AttributeError(f"module 'redirecall.main' has no attribute {name!r}")


def __dir__():
    names = set(globals())
    for _m in _MODULES:
        names.update(n for n in dir(_m) if not n.startswith("__"))
    return sorted(names)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cli() -> None:
    """Run the RediRecall server (the ``redirecall`` console script).

    Host/port come from REDIRECALL_HOST / REDIRECALL_PORT (default
    127.0.0.1:8420). Bound to loopback by default — there is no built-in auth
    yet, so front it with a reverse proxy before exposing it to a network.
    """
    import argparse
    import uvicorn

    # Parse BEFORE anything else: this function used to ignore sys.argv entirely
    # and go straight to uvicorn.run(), so `redirecall --help` started a server
    # and hung the terminal instead of printing help, and `--port` was silently
    # discarded. Argument handling must never fall through to starting a server.
    parser = argparse.ArgumentParser(
        prog="redirecall",
        description="Self-hosted retrieval-augmented chat over your own documents, "
                    "backed by Redis vector search.",
        epilog="Bound to loopback by default — there is no built-in auth, so front "
               "it with a reverse proxy before exposing it to a network.",
    )
    parser.add_argument("--host", default=os.environ.get("REDIRECALL_HOST", "127.0.0.1"),
                        help="interface to bind (env REDIRECALL_HOST, default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("REDIRECALL_PORT", "8420")),
                        help="port to listen on (env REDIRECALL_PORT, default 8420)")
    parser.add_argument("--version", action="version",
                        version=f"redirecall {__version__}")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    cli()

