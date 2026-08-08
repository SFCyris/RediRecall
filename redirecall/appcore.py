# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.appcore — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import os
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FASTAPI APP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

app = FastAPI(title="RediRecall")

# CORS — same-origin by default. The bundled UI is served from this app, so it
# needs no cross-origin grant and none is given (a wildcard would let any site
# script the API from a user's browser). Set REDIRECALL_CORS_ORIGINS to a
# comma-separated list of trusted origins only if you host the UI separately.
_cors_origins = [
    o.strip() for o in os.environ.get("REDIRECALL_CORS_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Security headers on every response — defense in depth alongside the frontend's
# DOMPurify sanitisation. 'unsafe-inline' is unavoidable (the single-file UI uses
# an inline <script>, inline styles, and static inline handlers), but the policy
# still blocks external script injection, plugins, framing, and cross-origin
# data exfiltration. cdnjs is allowed for marked / KaTeX / DOMPurify.
_CSP = (
    "default-src 'self'; "
    # cdnjs: marked/DOMPurify/KaTeX/abcjs/math.js + the lazy-loaded renderers
    # (mermaid, Chart.js, highlight.js, JSXGraph, Leaflet, Plotly).
    # jsDelivr: smiles-drawer (molecules) and @viz-js/viz (Graphviz) — neither is
    # published on cdnjs at a current version.
    # 'wasm-unsafe-eval': @viz-js/viz 3.x is a WebAssembly build (the old viz.js
    # 2.1.2 was asm.js). Without it the script loads but WebAssembly.instantiate()
    # is blocked and the ```dot lane dies with a CompileError. This directive
    # permits WebAssembly compilation ONLY — it does not enable eval() or
    # new Function().
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' "
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    # https: is needed for map tiles (e.g. OpenStreetMap) in the ```map lane.
    "img-src 'self' data: blob: https:; "
    "font-src 'self' https://cdnjs.cloudflare.com data:; "
    # paulrosen.github.io: abcjs fetches soundfont samples at play time for the
    # ```abc Play button. Scoped to that one host rather than opening connect-src.
    "connect-src 'self' https://paulrosen.github.io; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

