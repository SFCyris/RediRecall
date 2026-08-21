# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.routes_misc — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import copy
import json
import time
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import appcore, config, constants, ingest, sessions, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — FEEDBACK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/feedback")
async def api_feedback(payload: dict):
    # The whole store is rewritten per rating, so an unbounded body permanently
    # raises the cost of every later write. Truncate rather than reject: a long
    # answer should still be reviewable.
    entry = {
        k: (v[:config._MAX_FEEDBACK_FIELD] if isinstance(v, str) else v)
        for k, v in payload.items()
    }
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    state._feedback.append(entry)
    del state._feedback[:-config._MAX_FEEDBACK]        # bound memory as well as the file
    with open(constants.FEEDBACK_PATH, "w") as f:
        json.dump(state._feedback, f)
    return {"ok": True}


@appcore.app.get("/api/feedback")
def api_feedback_list(limit: int = 200, value: str | None = None):
    """Return stored ratings, newest first.

    The store was previously write-only — nothing read it back, so thumbs-down
    ratings could not be reviewed or turned into a regression set. ``value``
    filters to one rating (e.g. ``down``).
    """
    # Accept the words the docstring advertises as well as the raw 1/-1 the client
    # actually posts; "?value=down" previously matched nothing at all.
    _ALIASES = {"down": {"-1"}, "up": {"1"}, "negative": {"-1"}, "positive": {"1"}}
    wanted = _ALIASES.get((value or "").strip().lower(), {str(value)})
    items = state._feedback if not value else [f for f in state._feedback if str(f.get("value")) in wanted]
    # max(0, limit) was a bypass: items[-0:] is items[0:], i.e. everything.
    n = max(1, min(int(limit or 0) or 1, config._MAX_FEEDBACK))
    return {"total": len(items), "items": list(reversed(items[-n:]))}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — SESSIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/sessions")
def api_sessions():
    # In-memory sessions (active connections)
    result = []
    for sid, msgs in state._sessions.items():
        preview = ""
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                preview = m.get("content", "")[:60]
                break
        result.append({"id": sid, "messages": len(msgs), "preview": preview})
    # Merge in Redis-persisted sessions not currently loaded in memory
    result.extend(sessions.list_sessions_from_redis())
    return result

@appcore.app.get("/api/sessions/{sid}")
def api_session(sid: str):
    if sid in state._sessions:
        return state._sessions[sid]
    # Try Redis
    msgs = sessions.load_session(sid)
    if msgs:
        state._sessions[sid] = msgs   # cache in memory
    return msgs

@appcore.app.delete("/api/sessions/{sid}")
def api_delete_session(sid: str):
    state._sessions.pop(sid, None)
    sessions.delete_session_from_redis(sid)
    return {"ok": True}


@appcore.app.post("/api/sessions/{sid}/fork")
def api_fork_session(sid: str, payload: dict):
    """Fork a conversation: a new session holding the messages up to and
    including the anchored one. The original is untouched; the fork continues
    independently from that point.

    The anchor is {role, content_prefix, occurrence} rather than a bare index:
    the client's message array can run AHEAD of the stored one (aborted and
    errored streams are kept client-side but never persisted), so a client
    index would silently fork at the wrong message after any Stop. Content is
    identical for every turn both sides persisted, and turns only the client
    has can never match, so occurrence counting stays aligned."""
    msgs = state._sessions.get(sid) or sessions.load_session(sid)
    if not msgs:
        raise HTTPException(404, "session not found")
    role = payload.get("role")
    prefix = payload.get("content_prefix")
    occurrence = payload.get("occurrence", 1)
    if role not in ("user", "assistant") or not isinstance(prefix, str) or not prefix:
        raise HTTPException(400, "role and content_prefix are required")
    if not isinstance(occurrence, int) or occurrence < 1:
        raise HTTPException(400, "occurrence must be a positive int")
    at, seen = None, 0
    for i, m in enumerate(msgs):
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if m.get("role") == role and c.startswith(prefix):
            seen += 1
            if seen == occurrence:
                at = i
                break
    if at is None:
        raise HTTPException(409, "message not found in the stored session "
                                 "(it may not have been persisted — e.g. an aborted answer)")
    new_sid = f"sess_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_fork"
    state._sessions[new_sid] = [copy.deepcopy(m) for m in msgs[:at + 1]]
    state.touch_session(new_sid)
    sessions.save_session(new_sid, state._sessions[new_sid])
    return {"id": new_sid, "messages": len(state._sessions[new_sid])}


@appcore.app.get("/api/usage")
def api_usage():
    """All-time provider-reported token usage, per provider:model."""
    return sessions.usage_totals()


@appcore.app.delete("/api/usage")
def api_usage_clear():
    """Reset the all-time token-usage tally (Analytics → Token Usage → Reset)."""
    sessions.clear_usage()
    return {"ok": True}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — PROMPT TEMPLATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/templates")
def api_templates():
    return state._config.get("prompt_templates", [])

@appcore.app.post("/api/templates")
async def api_save_templates(payload: list):
    state._config["prompt_templates"] = payload
    config.save_config(state._config)
    return {"ok": True}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE UPLOAD — CHAT CONTEXT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/chat/upload-file")
async def api_chat_upload_file(file: UploadFile = File(...)):
    """Extract plain text from an uploaded document (TXT, MD, CSV, PDF, DOCX, XLSX).

    The extracted text is returned to the browser and included in the next chat
    message as inline context — similar to pasting the document into the prompt,
    but with server-side format conversion.  Files are NOT stored on disk or
    indexed into Redis; they exist only for the duration of the browser session.
    """
    data = await file.read()
    if len(data) > ingest._CHAT_FILE_MAX_BYTES:
        raise HTTPException(413, f"File too large (max {ingest._CHAT_FILE_MAX_BYTES // (1024*1024)} MB)")
    try:
        text = await asyncio.to_thread(ingest.extract_file_text, file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"Could not parse file: {e}")
    truncated = len(text) > ingest._CHAT_FILE_MAX_CHARS
    if truncated:
        text = text[:ingest._CHAT_FILE_MAX_CHARS]
    return {
        "filename": file.filename,
        "chars":    len(text),
        "truncated": truncated,
        "text":     text,
    }


