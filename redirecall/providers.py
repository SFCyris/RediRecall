# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.providers — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import base64
import json
import time
from pathlib import Path
import httpx
try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False
try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False
try:
    from openai import AsyncOpenAI as _AsyncOpenAI
    _OPENAI_SDK_AVAILABLE = True
except ImportError:
    _OPENAI_SDK_AVAILABLE = False
from . import config, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM PROVIDER — OLLAMA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ollama_base() -> str:
    """Return the base URL for the configured Ollama server."""
    cfg = state._config.get("ollama", {})
    host = cfg.get("host", "http://localhost").rstrip("/")
    port = cfg.get("port", 11434)
    return f"{host}:{port}"

# Cache the model list for 30 s to avoid hammering Ollama on concurrent calls.
_OLLAMA_MODELS_TTL = 30.0


async def ollama_models() -> list[dict]:
    """
    Fetch available models from the Ollama server.
    Returns a list of {name, size, vision, details} dicts.
    Vision detection checks model family tags and common naming conventions.
    Result is cached for _OLLAMA_MODELS_TTL seconds to avoid hammering Ollama
    when /api/status/ollama and /api/ollama/models are called concurrently.
    """
    if time.time() - state._ollama_models_ts < _OLLAMA_MODELS_TTL and state._ollama_models_cache:
        return state._ollama_models_cache
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(f"{ollama_base()}/api/tags")
            data = res.json()
            models = []
            for m in data.get("models", []):
                name = m["name"]
                details = m.get("details", {})
                families = details.get("families", []) or []
                vision = any(f in ["clip", "llava"] for f in families) or any(
                    v in name.lower()
                    for v in ["llava", "bakllava", "moondream", "vision", "minicpm", "gemma3", "qwen-vl"]
                )
                models.append({"name": name, "size": m.get("size", 0), "vision": vision, "details": details})
            state._ollama_models_cache = models
            state._ollama_models_ts = time.time()
            return models
    except Exception as e:
        log.error(f"Ollama models error: {e}")
        return []


_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

def _tool_result_to_markdown(tool_calls: list) -> str:
    """
    Convert Ollama tool_call entries into renderable markdown.
    If a tool result looks like an image (data URI, local path, or HTTP URL),
    emit markdown that the frontend can detect and render.
    """
    parts = []
    for tc in tool_calls:
        fn   = tc.get("function", {})
        name = fn.get("name", "tool")
        args = fn.get("arguments", {})

        # Check if any argument value looks like an image
        for val in (args.values() if isinstance(args, dict) else []):
            val = str(val).strip()
            # Data URI
            if val.startswith("data:image/"):
                parts.append(f"\n![{name} result]({val})\n")
                continue
            # Local file path with image extension
            p = Path(val)
            if p.suffix.lower() in _IMG_EXTS and p.is_file():
                parts.append(f"\n![{name} result](/api/files/image?path={val})\n")
                continue
            # HTTP URL pointing to an image
            if val.startswith(("http://", "https://")) and Path(val).suffix.lower() in _IMG_EXTS:
                parts.append(f"\n![{name} result]({val})\n")
                continue
        else:
            # No image found — emit the raw tool call as a code block
            parts.append(f"\n```json\n[tool: {name}] {json.dumps(args, ensure_ascii=False)}\n```\n")
    return "".join(parts)


def _to_ollama_messages(messages: list) -> list:
    """
    Convert OpenAI-style messages to Ollama format.

    OpenAI multi-modal content:
        [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}]

    Ollama expects:
        {"role": "user", "content": "...", "images": ["<raw_base64>"]}

    Non-multi-modal messages are passed through unchanged.
    """
    result = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            text_parts: list[str] = []
            b64_images: list[str] = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part["text"])
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if "base64," in url:
                        b64_images.append(url.split("base64,", 1)[1])
            msg: dict = {"role": m["role"], "content": " ".join(text_parts)}
            if b64_images:
                msg["images"] = b64_images
        else:
            msg = {"role": m["role"], "content": content}
        result.append(msg)
    return result


async def ollama_stream(messages: list, model: str, images: list[str] | None = None):
    """
    Stream tokens from Ollama /api/chat endpoint.
    Yields (token: str, done: bool) tuples.

    Handles both plain text responses and tool_call responses:
    - Plain content tokens are yielded directly.
    - tool_calls entries are converted to renderable markdown (images or code blocks).
    """
    payload = {"model": model, "messages": _to_ollama_messages(messages), "stream": True}
    url = f"{ollama_base()}/api/chat"
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    d    = json.loads(line)
                    msg  = d.get("message", {})
                    done = d.get("done", False)

                    # Standard text content
                    token = msg.get("content", "")

                    # Tool calls — convert to markdown so the frontend can render images
                    tool_calls = msg.get("tool_calls", [])
                    if tool_calls:
                        token += _tool_result_to_markdown(tool_calls)

                    if token:
                        yield token, done
                    elif done:
                        yield "", True
                    if done:
                        break
                except Exception:
                    pass

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM PROVIDER — ANTHROPIC CLAUDE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Static model list (Claude API doesn't have a /models endpoint that lists them)
CLAUDE_MODELS = [
    {"id": "claude-opus-4-6",           "name": "Claude Opus 4.6",   "context": 200000},
    {"id": "claude-sonnet-4-6",         "name": "Claude Sonnet 4.6", "context": 200000},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5",  "context": 200000},
]


def _to_claude_content(content):
    """
    Convert OpenAI-style message content to Claude API format.

    OpenAI image:  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
    Claude image:  {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}

    Plain strings pass through unchanged.
    """
    if not isinstance(content, list):
        return content
    result = []
    for part in content:
        if part.get("type") == "text":
            result.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            if "base64," in url:
                header, data = url.split("base64,", 1)
                media_type = header.rstrip(";").split(":")[-1]  # e.g. "image/jpeg"
                result.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
    return result


async def claude_stream(messages: list, model: str):
    """
    Stream tokens from Anthropic Claude using the native anthropic SDK.
    Yields (token: str, done: bool) tuples.
    Falls back to a clear error if the SDK is not installed.
    """
    api_key  = state._config.get("claude", {}).get("api_key", "")
    base_url = state._config.get("claude", {}).get("base_url", "https://api.anthropic.com").rstrip("/")

    if not api_key:
        yield "Error: Claude API key not configured.", True
        return
    if not _ANTHROPIC_AVAILABLE:
        yield "Error: anthropic package not installed. Run: pip install anthropic", True
        return

    system_msg, claude_msgs = "", []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"] if isinstance(m["content"], str) else str(m["content"])
        else:
            claude_msgs.append({"role": m["role"], "content": _to_claude_content(m["content"])})

    try:
        client = config._cached_client("anthropic", api_key, base_url)
        kwargs: dict = {"model": model, "messages": claude_msgs, "max_tokens": 4096}
        if system_msg:
            kwargs["system"] = system_msg
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text, False
        yield "", True
    except _anthropic.APIStatusError as e:
        yield f"Error: {e.message}", True
    except Exception as e:
        yield f"Claude error: {e}", True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM PROVIDER — OPENAI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Models we expose in the UI.  Fetched dynamically from /v1/models when possible
# but kept as a static fallback so the UI always has something to show.
OPENAI_MODELS_STATIC = [
    {"id": "gpt-4o",            "name": "GPT-4o",               "context": 128000},
    {"id": "gpt-4o-mini",       "name": "GPT-4o mini",          "context": 128000},
    {"id": "gpt-4.1",           "name": "GPT-4.1",              "context": 1047576},
    {"id": "gpt-4.1-mini",      "name": "GPT-4.1 mini",         "context": 1047576},
    {"id": "gpt-4.1-nano",      "name": "GPT-4.1 nano",         "context": 1047576},
    {"id": "o1",                "name": "o1",                   "context": 200000},
    {"id": "o3",                "name": "o3",                   "context": 200000},
    {"id": "o3-mini",           "name": "o3-mini",              "context": 200000},
    {"id": "o4-mini",           "name": "o4-mini",              "context": 200000},
]


async def openai_models() -> list[dict]:
    """
    Fetch the list of available models from the OpenAI /v1/models endpoint.
    Uses the native openai SDK. Falls back to the static list on any error.
    Only returns chat-capable models (id starts with 'gpt-' or 'o').
    """
    api_key  = state._config.get("openai", {}).get("api_key", "")
    base_url = state._config.get("openai", {}).get("base_url", "https://api.openai.com").rstrip("/")
    if not api_key or not _OPENAI_SDK_AVAILABLE:
        return OPENAI_MODELS_STATIC

    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        page = await client.models.list()
        models = []
        for m in sorted(page.data, key=lambda x: x.id):
            mid = m.id
            if mid.startswith(("gpt-4o", "gpt-4.1", "o1", "o3", "o4")):
                name = next((x["name"] for x in OPENAI_MODELS_STATIC if x["id"] == mid), mid)
                ctx  = next((x["context"] for x in OPENAI_MODELS_STATIC if x["id"] == mid), 128000)
                models.append({"id": mid, "name": name, "context": ctx})
        return models if models else OPENAI_MODELS_STATIC
    except Exception as e:
        log.error(f"OpenAI models error: {e}")
        return OPENAI_MODELS_STATIC


async def openai_stream(messages: list, model: str):
    """
    Stream tokens from the OpenAI /v1/chat/completions endpoint using the native openai SDK.
    Yields (token: str, done: bool) tuples — same interface as claude_stream.

    Compatible with any OpenAI-compatible API by changing the base_url in settings.
    """
    api_key  = state._config.get("openai", {}).get("api_key", "")
    base_url = state._config.get("openai", {}).get("base_url", "https://api.openai.com").rstrip("/")

    if not api_key:
        yield "Error: OpenAI API key not configured.", True
        return
    if not _OPENAI_SDK_AVAILABLE:
        yield "Error: openai package not installed. Run: pip install openai", True
        return

    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        stream = await client.chat.completions.create(
            model=model, messages=messages, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content, False
            if chunk.choices[0].finish_reason:
                break
        yield "", True
    except Exception as e:
        yield f"OpenAI error: {e}", True


# ── Qwen / DashScope ──────────────────────────────────────────────────────────
# DashScope exposes an OpenAI-compatible /v1/chat/completions endpoint.
# Free-tier models: qwen-plus, qwen-turbo, qwen-long (generous monthly quotas).
# Premium: qwen-max, qwen-max-longcontext.
# Get a free API key at: https://qwen.ai

QWEN_MODELS_STATIC = [
    {"id": "qwen-plus",              "name": "Qwen Plus (free tier)",       "context": 131072},
    {"id": "qwen-turbo",             "name": "Qwen Turbo (free tier)",      "context": 131072},
    {"id": "qwen-long",              "name": "Qwen Long (free tier)",       "context": 10000000},
    {"id": "qwen-max",               "name": "Qwen Max",                    "context": 131072},
    {"id": "qwen-max-longcontext",   "name": "Qwen Max (long ctx)",         "context": 1000000},
    {"id": "qwen2.5-72b-instruct",   "name": "Qwen 2.5 72B Instruct",      "context": 131072},
    {"id": "qwen2.5-7b-instruct",    "name": "Qwen 2.5 7B Instruct",       "context": 131072},
    {"id": "qwen2.5-coder-32b-instruct", "name": "Qwen 2.5 Coder 32B",     "context": 131072},
]


async def qwen_stream(messages: list, model: str):
    """
    Stream tokens from Alibaba DashScope using the openai SDK with DashScope's base URL.
    Yields (token: str, done: bool) — same interface as openai_stream / claude_stream.

    Free-tier models: qwen-plus, qwen-turbo, qwen-long.
    API key: get a free key at qwen.ai (DASHSCOPE_API_KEY env var supported).
    """
    api_key  = state._config.get("qwen", {}).get("api_key", "")
    base_url = state._config.get("qwen", {}).get("base_url",
                           "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

    if not api_key:
        yield "Error: Qwen API key not configured. Get a free key at qwen.ai", True
        return
    if not _OPENAI_SDK_AVAILABLE:
        yield "Error: openai package not installed. Run: pip install openai", True
        return

    try:
        # DashScope base_url already includes /v1 path — pass it directly to the SDK
        client = config._cached_client("openai", api_key, base_url)
        stream = await client.chat.completions.create(
            model=model, messages=messages, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content, False
            if chunk.choices[0].finish_reason:
                break
        yield "", True
    except Exception as e:
        yield f"Qwen error: {e}", True

# ── Mistral ─────────────────────────────────────────────────────────────────────
# OpenAI-compatible endpoint (api.mistral.ai/v1). EU-hosted. The free "Experiment"
# tier covers every model with generous limits and needs no credit card (phone
# verification only) — https://console.mistral.ai.
MISTRAL_MODELS_STATIC = [
    {"id": "mistral-small-latest",   "name": "Mistral Small (free tier)",   "context": 32000},
    {"id": "open-mistral-nemo",      "name": "Mistral Nemo (free tier)",    "context": 128000},
    {"id": "mistral-large-latest",   "name": "Mistral Large",               "context": 128000},
    {"id": "codestral-latest",       "name": "Codestral (code)",            "context": 256000},
    {"id": "ministral-8b-latest",    "name": "Ministral 8B",                "context": 128000},
    {"id": "ministral-3b-latest",    "name": "Ministral 3B",                "context": 128000},
    {"id": "pixtral-12b-2409",       "name": "Pixtral 12B (vision)",        "context": 128000},
]

async def mistral_stream(messages: list, model: str):
    """
    Stream tokens from Mistral La Plateforme via the openai SDK (Mistral is
    OpenAI-compatible). Yields (token: str, done: bool) — same interface as the others.

    Free "Experiment" tier: all models, ~1B tokens/month, no credit card.
    API key: https://console.mistral.ai (MISTRAL_API_KEY env var supported).
    """
    api_key  = state._config.get("mistral", {}).get("api_key", "")
    base_url = state._config.get("mistral", {}).get("base_url",
                           "https://api.mistral.ai/v1").rstrip("/")

    if not api_key:
        yield "Error: Mistral API key not configured. Get a free key at console.mistral.ai", True
        return
    if not _OPENAI_SDK_AVAILABLE:
        yield "Error: openai package not installed. Run: pip install openai", True
        return

    try:
        # Mistral's base_url already includes /v1 — pass it directly to the SDK.
        client = config._cached_client("openai", api_key, base_url)
        stream = await client.chat.completions.create(
            model=model, messages=messages, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content, False
            if chunk.choices[0].finish_reason:
                break
        yield "", True
    except Exception as e:
        yield f"Mistral error: {e}", True

# ── Groq ──────────────────────────────────────────────────────────────────────
# OpenAI-compatible endpoint; very fast inference; generous free tier.
# Get a free API key at console.groq.com (no credit card required).

GROQ_MODELS_STATIC = [
    {"id": "llama-3.3-70b-versatile",  "name": "Llama 3.3 70B (free)",        "context": 128000},
    {"id": "llama-3.1-8b-instant",     "name": "Llama 3.1 8B Instant (free)", "context": 131072},
    {"id": "llama3-70b-8192",          "name": "Llama 3 70B (free)",          "context": 8192},
    {"id": "llama3-8b-8192",           "name": "Llama 3 8B (free)",           "context": 8192},
    {"id": "mixtral-8x7b-32768",       "name": "Mixtral 8×7B (free)",         "context": 32768},
    {"id": "gemma2-9b-it",             "name": "Gemma 2 9B (free)",           "context": 8192},
]


async def groq_models() -> list[dict]:
    """Fetch available models from Groq using the openai SDK (Groq is OpenAI-compatible)."""
    api_key  = state._config.get("groq", {}).get("api_key", "")
    base_url = state._config.get("groq", {}).get("base_url", "https://api.groq.com/openai").rstrip("/")
    if not api_key or not _OPENAI_SDK_AVAILABLE:
        return GROQ_MODELS_STATIC
    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        page = await client.models.list()
        models = []
        for m in sorted(page.data, key=lambda x: x.id):
            mid = m.id
            if not mid:
                continue
            name = next((x["name"] for x in GROQ_MODELS_STATIC if x["id"] == mid), mid)
            ctx  = next((x["context"] for x in GROQ_MODELS_STATIC if x["id"] == mid),
                        getattr(m, "context_window", None) or 8192)
            models.append({"id": mid, "name": name, "context": ctx})
        return models if models else GROQ_MODELS_STATIC
    except Exception as e:
        log.error(f"Groq models error: {e}")
        return GROQ_MODELS_STATIC


async def groq_stream(messages: list, model: str):
    """
    Stream tokens from Groq using the openai SDK (Groq is fully OpenAI-compatible).
    Yields (token: str, done: bool) — same interface as openai_stream.
    """
    api_key  = state._config.get("groq", {}).get("api_key", "")
    base_url = state._config.get("groq", {}).get("base_url", "https://api.groq.com/openai").rstrip("/")

    if not api_key:
        yield "Error: Groq API key not configured. Get a free key at console.groq.com", True
        return
    if not _OPENAI_SDK_AVAILABLE:
        yield "Error: openai package not installed. Run: pip install openai", True
        return

    try:
        client = config._cached_client("openai", api_key, f"{base_url}/v1")
        stream = await client.chat.completions.create(
            model=model, messages=messages, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content, False
            if chunk.choices[0].finish_reason:
                break
        yield "", True
    except Exception as e:
        err = str(e)
        if "429" in err or "rate_limit" in err.lower():
            yield ("Error: Groq rate limit reached (free tier has per-minute token limits). "
                   "Wait a moment and try again, or switch to a smaller model like llama-3.1-8b-instant."), True
        else:
            yield f"Groq error: {e}", True


# ── Google Gemini ──────────────────────────────────────────────────────────────
# Uses the native google-genai SDK (pip install google-genai).
# Get a free API key at aistudio.google.com · Set GEMINI_API_KEY env var.

GEMINI_MODELS_STATIC = [
    {"id": "gemini-3-flash-preview",        "name": "Gemini 3 Flash Preview",   "context": 1048576},
    {"id": "gemini-2.5-pro-preview-03-25",  "name": "Gemini 2.5 Pro Preview",   "context": 1048576},
    {"id": "gemini-2.5-flash-preview-04-17","name": "Gemini 2.5 Flash Preview", "context": 1048576},
    {"id": "gemini-2.0-flash",              "name": "Gemini 2.0 Flash",         "context": 1048576},
    {"id": "gemini-2.0-flash-lite",         "name": "Gemini 2.0 Flash Lite",    "context": 1048576},
    {"id": "gemini-1.5-flash",              "name": "Gemini 1.5 Flash",         "context": 1048576},
    {"id": "gemini-1.5-flash-8b",           "name": "Gemini 1.5 Flash 8B",      "context": 1048576},
    {"id": "gemini-1.5-pro",                "name": "Gemini 1.5 Pro",           "context": 2097152},
]


def _to_gemini_contents(messages: list) -> tuple[list, str | None]:
    """
    Convert OpenAI-format messages to Gemini native content objects.
    Returns (contents, system_instruction_text).
    Roles: OpenAI "assistant" → Gemini "model". System messages → system_instruction.
    """
    if not _GENAI_AVAILABLE:
        return [], None
    system_parts: list[str] = []
    contents = []
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            txt = " ".join(p.get("text", "") for p in content if p.get("type") == "text") \
                  if isinstance(content, list) else content
            system_parts.append(txt)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "text":
                    parts.append(_genai_types.Part.from_text(text=part["text"]))
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if "base64," in url:
                        header, data = url.split("base64,", 1)
                        mime = header.rstrip(";").split(":")[-1]
                        parts.append(_genai_types.Part.from_bytes(
                            data=base64.b64decode(data), mime_type=mime))
        else:
            parts = [_genai_types.Part.from_text(text=content)]
        contents.append(_genai_types.Content(role=gemini_role, parts=parts))
    system_text = "\n\n".join(system_parts) if system_parts else None
    return contents, system_text


def _gemini_err_msg(exc: Exception) -> str:
    """Return a human-readable error message from a Gemini SDK exception."""
    s = str(exc)
    if "limit: 0" in s or "free_tier" in s.lower():
        return ("Gemini free-tier quota is zero for this project. "
                "Enable billing at console.cloud.google.com or create a new project at aistudio.google.com.")
    if "RESOURCE_EXHAUSTED" in s or "quota" in s.lower() or "429" in s:
        return "Gemini quota exhausted. Wait for daily reset (midnight Pacific) or enable billing."
    if "API_KEY_INVALID" in s or "401" in s or "403" in s:
        return "Gemini API key is invalid or revoked. Enter a new key in Settings → Gemini."
    if "NOT_FOUND" in s or "404" in s:
        return "Gemini model not found. Open Settings → Gemini, click Refresh Models and pick another."
    return f"Gemini error: {exc}"


async def gemini_models() -> list[dict]:
    """Fetch available Gemini models via the native SDK. Falls back to static list."""
    api_key = state._config.get("gemini", {}).get("api_key", "")
    if not api_key or not _GENAI_AVAILABLE:
        return GEMINI_MODELS_STATIC
    try:
        client = _google_genai.Client(api_key=api_key)
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: list(client.models.list()))
        result = []
        for m in sorted(raw, key=lambda x: getattr(x, "name", "")):
            mid = getattr(m, "name", "")
            if mid.startswith("models/"):
                mid = mid[7:]
            if not mid.startswith("gemini"):
                continue
            name = next((x["name"] for x in GEMINI_MODELS_STATIC if x["id"] == mid), mid)
            ctx  = next((x["context"] for x in GEMINI_MODELS_STATIC if x["id"] == mid), 1048576)
            result.append({"id": mid, "name": name, "context": ctx})
        return result if result else GEMINI_MODELS_STATIC
    except Exception as e:
        log.error(f"Gemini models error: {e}")
        return GEMINI_MODELS_STATIC


async def gemini_stream(messages: list, model: str):
    """
    Stream tokens from Google Gemini using the native google-genai SDK.
    Yields (token: str, done: bool) — same interface as openai_stream / claude_stream.
    """
    api_key = state._config.get("gemini", {}).get("api_key", "")
    if not api_key:
        yield "Error: Gemini API key not configured. Get a free key at aistudio.google.com", True
        return
    if not _GENAI_AVAILABLE:
        yield "Error: google-genai package not installed. Run: pip install google-genai", True
        return

    try:
        client   = _google_genai.Client(api_key=api_key)
        contents, system_text = _to_gemini_contents(messages)
        cfg = _genai_types.GenerateContentConfig(
            system_instruction=system_text if system_text else None,
        )
        log.info(f"Gemini request: model={model!r} turns={len(contents)}")
        async for chunk in await client.aio.models.generate_content_stream(
            model=model, contents=contents, config=cfg,
        ):
            text = getattr(chunk, "text", None)
            if text:
                yield text, False
        yield "", True
    except Exception as e:
        log.warning(f"Gemini stream error: {e}")
        yield _gemini_err_msg(e), True


