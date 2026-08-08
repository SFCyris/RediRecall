# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.ws — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import time
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import config, crawler, rag_admin, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEBSOCKET MANAGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConnManager:
    """Tracks active WebSocket connections by session ID."""

    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, ws: WebSocket, sid: str):
        await ws.accept()
        self.active[sid] = ws

    def disconnect(self, sid: str):
        self.active.pop(sid, None)

    async def send(self, sid: str, data: dict):
        ws = self.active.get(sid)
        if ws:
            await ws.send_json(data)


mgr = ConnManager()

async def _recrawl_loop():
    """
    Background task: periodically re-crawl all scheduled web sources.
    Runs every 60 seconds internally; actual crawls only trigger when
    ``now - last_crawled >= interval_minutes * 60``.
    """
    while True:
        await asyncio.sleep(60)
        if not state._config.get("recrawl", {}).get("enabled", False):
            continue
        interval_secs = int(state._config.get("recrawl", {}).get("interval_minutes", 60)) * 60
        now = time.time()
        scheduled = state._config.get("scheduled_sources", [])
        if not scheduled:
            continue
        changed = False
        for src in scheduled:
            last = float(src.get("last_crawled", 0))
            if now - last < interval_secs:
                continue
            url      = src.get("url", "").strip()
            instance = src.get("instance", "default")
            depth    = int(src.get("depth", 0))
            if not url:
                continue
            log.info(f"Recrawl scheduler: crawling {url} → instance '{instance}'")
            try:
                rc = rag_admin.rc_for_instance(instance)
                await crawler.crawl_url(instance, url, depth, rc=rc)
                src["last_crawled"] = now
                changed = True
            except Exception as e:
                log.warning(f"Recrawl failed for {url}: {e}")
        if changed:
            config.save_config(state._config)


