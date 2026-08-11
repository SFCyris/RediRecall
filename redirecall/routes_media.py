# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_media — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from . import appcore, providers

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — LOCAL FILE IMAGE PROXY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import mimetypes
import tempfile

_ALLOWED_IMAGE_DIRS: list[Path] = [
    Path(tempfile.gettempdir()),   # /tmp — tool-generated images usually land here
    Path.cwd(),                    # project working directory
]

@appcore.app.get("/api/files/image")
async def api_serve_image(path: str):
    """
    Proxy for locally-generated image files produced by Ollama tools.
    Only serves files that:
      - Exist on disk
      - Have an image extension (png, jpg, gif, webp, bmp)
      - Are located inside an allowed directory (tmp or cwd) to prevent
        arbitrary filesystem access.
    """
    try:
        p = Path(path).resolve()
    except Exception:
        raise HTTPException(400, "Invalid path")

    if p.suffix.lower() not in providers._IMG_EXTS:
        raise HTTPException(400, "Not an image file")

    if not p.is_file():
        raise HTTPException(404, "File not found")

    # Security: only serve from allowed directories
    allowed = any(
        str(p).startswith(str(d.resolve()))
        for d in _ALLOWED_IMAGE_DIRS
    )
    if not allowed:
        raise HTTPException(403, "Path not in an allowed directory")

    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"
    # identity => GZipMiddleware forwards untouched. Images are already compressed
    # (PNG/JPEG/WebP), so a max-effort gzip pass costs CPU for ~0 byte gain.
    return FileResponse(str(p), media_type=mime,
                        headers={"Content-Encoding": "identity"})

