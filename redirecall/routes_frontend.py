# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_frontend — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
from pathlib import Path
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from . import appcore

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERVE FRONTEND
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Serve index.html from beside this module (not the current working directory),
# so the app works regardless of where it is launched from.
_INDEX_HTML = Path(__file__).resolve().parent / "index.html"
if _INDEX_HTML.exists():
    @appcore.app.get("/")
    def serve_index():
        # no-cache so a rebuilt/edited UI is always picked up on reload (the
        # single-file frontend changes far more often than it's worth caching).
        return FileResponse(str(_INDEX_HTML), headers={"Cache-Control": "no-cache, must-revalidate"})


