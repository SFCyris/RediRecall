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
# TOKEN USAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Every *_stream() accepts an optional `usage` dict the caller owns. When the
# provider's final chunk reports real token counts, the stream fills it in place
# — a mutable sink, because the yield contract is (token, done) tuples and a
# generator cannot also return a value to an `async for` consumer.

def _sink_usage(sink: dict | None, prompt, completion, cached=None) -> None:
    """Record provider-reported token counts into the caller's sink dict.
    Only writes when both counts are real non-negative ints — a partial record
    would be mistaken downstream for a measured turn."""
    if sink is None:
        return
    try:
        p, c = int(prompt), int(completion)
    except (TypeError, ValueError):
        return
    if p < 0 or c < 0:
        return
    sink["prompt"], sink["completion"] = p, c
    if cached is not None:
        try:
            sink["cached"] = max(0, int(cached))
        except (TypeError, ValueError):
            pass


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

        # Check if any argument value looks like an image. The image branches used to
        # `continue`, which does not end a for/else — so the else fired every time and
        # the raw JSON (including the whole base64 data URI) was appended after the
        # image markdown on every call. Track the hit explicitly instead.
        found_image = False
        for val in (args.values() if isinstance(args, dict) else []):
            val = str(val).strip()
            # Data URI
            if val.startswith("data:image/"):
                parts.append(f"\n![{name} result]({val})\n")
                found_image = True
                continue
            # Local file path with image extension
            p = Path(val)
            if p.suffix.lower() in _IMG_EXTS and p.is_file():
                parts.append(f"\n![{name} result](/api/files/image?path={val})\n")
                found_image = True
                continue
            # HTTP URL pointing to an image
            if val.startswith(("http://", "https://")) and Path(val).suffix.lower() in _IMG_EXTS:
                parts.append(f"\n![{name} result]({val})\n")
                found_image = True
                continue
        if not found_image:
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


async def ollama_stream(messages: list, model: str, images: list[str] | None = None,
                        usage: dict | None = None):
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
                    if done:   # the final chunk carries the real token counts
                        _sink_usage(usage, d.get("prompt_eval_count"), d.get("eval_count"))

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
# Which hosted models accept an image part alongside the text prompt.
#
# Ollama answers this itself (family tags on /api/tags); no hosted provider exposes an
# equivalent field on its model list, so this is a table. It is deliberately biased
# towards False: a model wrongly marked False leaves 📎 disabled, which is exactly how
# every hosted provider behaved before this table existed. One wrongly marked True lets
# a user attach an image and collect an API error on send.
_VISION_PREFIXES = (
    "claude-",      # Claude 3 onward accepts image blocks on every model
    "gpt-4o", "gpt-4.1",
    "gemini-",      # the chat-capable Gemini families are all multimodal
    "pixtral-",
)
_VISION_EXACT = {"o1", "o3", "o4-mini"}   # note: o3-mini is text-only, so exact not prefix


def supports_vision(model_id: str) -> bool:
    """True when a hosted model accepts image input. Conservative — see _VISION_PREFIXES."""
    mid = (model_id or "").lower()
    return mid in _VISION_EXACT or mid.startswith(_VISION_PREFIXES)


def filter_mistral_models(data: list[dict]) -> list[dict]:
    """Keep the chat-capable entries from Mistral's /v1/models, sorted by id.

    Mistral reports per-model capabilities, so chat-capability and vision are answers
    rather than guesses. The route used to keep every id it was handed while its
    comment claimed the list was chat-only, so embedding, OCR, moderation and voxtral
    audio models all reached the model picker.
    """
    out = {}
    for m in data or []:
        mid = m.get("id")
        caps = m.get("capabilities") or {}
        if not mid or not caps.get("completion_chat"):
            continue
        # Prefer the curated label where there is one. The live endpoint returns bare
        # ids, so building the name from the id alone discarded the "(free tier)" hint
        # the static list carries — the same merge gemini_models already does.
        name = next((x["name"] for x in MISTRAL_MODELS_STATIC if x["id"] == mid), mid)
        out[mid] = {"id": mid, "name": name,
                    "context": m.get("max_context_length") or 0,
                    "vision": bool(caps.get("vision"))}
    return [out[i] for i in sorted(out)]


def stamp_vision(models: list[dict]) -> list[dict]:
    """Add a ``vision`` flag to hosted model dicts that do not already carry one.

    Mistral reports vision itself and is left alone; the rest fall back to the table.
    The frontend gates the 📎 button on this key, and a missing key reads as False.

    Returns fresh dicts. The hosted fetchers hand back the module-level *_STATIC lists
    on fallback, so stamping in place would permanently mutate those constants.
    """
    return [dict(m, vision=m.get("vision", supports_vision(m.get("id", ""))))
            for m in models]


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


async def claude_stream(messages: list, model: str, usage: dict | None = None):
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
            # Cache the system prefix: RAG/file context now rides on the user turn
            # (see handle_chat), so this base-instruction prefix is byte-stable across
            # a conversation and re-reads at ~0.1x input price on later turns. Below
            # the model's ~1024-token minimum it simply isn't cached (no error).
            kwargs["system"] = [{"type": "text", "text": system_msg,
                                 "cache_control": {"type": "ephemeral"}}]
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text, False
            try:
                # The accumulated final message carries the billed counts. Cache
                # reads are input billed at ~0.1x, cache WRITES at ~1.25x — both
                # reported separately so the cost estimate prices them correctly
                # (cache writes happen on the first turn and after every expiry).
                u = (await stream.get_final_message()).usage
                _sink_usage(usage, u.input_tokens, u.output_tokens,
                            getattr(u, "cache_read_input_tokens", 0))
                cw = getattr(u, "cache_creation_input_tokens", 0) or 0
                if usage is not None and "prompt" in usage and cw:
                    usage["cache_write"] = int(cw)
            except Exception:
                pass   # usage is best-effort; never fail a delivered answer over it
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


# Providers whose endpoint rejected stream_options once — skipped from then on,
# so a non-OpenAI base_url doesn't pay a failed round-trip on every message.
_NO_STREAM_OPTIONS: set = set()


async def _openai_compat_stream(provider_key: str, client, model: str, messages: list,
                                usage: dict | None, want_stream_options: bool):
    """The shared streaming loop for every OpenAI-SDK provider.

    Two deliberate differences from the old per-provider loops:
      * no `break` on finish_reason — the usage-bearing final chunk arrives AFTER
        it (with empty `choices`), so breaking early is exactly what would lose
        the counts. The stream closes itself right after.
      * usage is read from `chunk.usage` wherever the endpoint puts it (OpenAI
        with include_usage, Mistral and DashScope report it unprompted), plus
        Groq's `chunk.x_groq.usage` spelling.
    """
    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if want_stream_options and provider_key not in _NO_STREAM_OPTIONS:
        kwargs["stream_options"] = {"include_usage": True}
    try:
        stream = await client.chat.completions.create(**kwargs)
    except Exception as e:
        # Demote ONLY on an actual rejection of the parameter. A 401/429/DNS blip
        # here must surface as itself — permanently dropping include_usage over a
        # transient failure would silently kill usage reporting until restart.
        msg = str(e).lower()
        if "stream_options" not in kwargs:
            raise
        # The message has to implicate the parameter. Treating a bare 400 as a rejection
        # was the bug: a mistyped model name, an over-long context and a malformed image
        # are all 400s, and each one permanently disabled token counting for the whole
        # provider — the exact feature the user asked to be able to track.
        names_param = "stream_options" in msg or "include_usage" in msg
        status_400  = (getattr(e, "status_code", None) == 400
                       or "badrequest" in type(e).__name__.lower())
        if not (names_param or status_400):
            raise
        kwargs.pop("stream_options")
        try:
            stream = await client.chat.completions.create(**kwargs)
        except Exception as retry_err:
            # The retry failed too, so the 400 was about something else — surface the
            # ORIGINAL error, which describes the user's actual problem. Chain the retry
            # error rather than discarding it with `from None`: when the two differ (a 400
            # followed by a 429) the second one was vanishing silently, leaving no trace of
            # a rate limit anywhere.
            if type(retry_err) is not type(e) or str(retry_err) != str(e):
                log.warning(f"{provider_key}: retry without stream_options also failed "
                            f"({retry_err!r}); reporting the original error")
            raise e from retry_err
        if names_param:
            # Only a message that actually named the parameter is evidence the endpoint
            # does not support it. A bare 400 that happened to succeed on retry is not,
            # so that case retries each turn rather than poisoning the process.
            log.warning(f"{provider_key}: endpoint rejected stream_options "
                        f"({e}) — usage reporting off for this provider until restart")
            _NO_STREAM_OPTIONS.add(provider_key)
    # After finish_reason the only expected trailing chunk is the usage one; a
    # misbehaving OpenAI-compatible proxy that then holds the connection open
    # would otherwise hang the composer forever (the finish_reason `break` used
    # to be the terminator). 3 s is far above a same-connection trailing chunk.
    it = stream.__aiter__()
    finished = False
    while True:
        try:
            if finished:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=3.0)
            else:
                chunk = await it.__anext__()
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            break
        u = getattr(chunk, "usage", None) or getattr(getattr(chunk, "x_groq", None), "usage", None)
        if u is not None:
            _sink_usage(usage, getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))
        if not chunk.choices:
            continue
        if chunk.choices[0].finish_reason:
            finished = True
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content, False


async def openai_stream(messages: list, model: str, usage: dict | None = None):
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
        async for tok, done in _openai_compat_stream("openai", client, model, messages,
                                                     usage, want_stream_options=True):
            yield tok, done
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


async def qwen_stream(messages: list, model: str, usage: dict | None = None):
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
        async for tok, done in _openai_compat_stream("qwen", client, model, messages,
                                                     usage, want_stream_options=False):
            yield tok, done
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

async def mistral_stream(messages: list, model: str, usage: dict | None = None):
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
        async for tok, done in _openai_compat_stream("mistral", client, model, messages,
                                                     usage, want_stream_options=False):
            yield tok, done
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


async def groq_stream(messages: list, model: str, usage: dict | None = None):
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
        async for tok, done in _openai_compat_stream("groq", client, model, messages,
                                                     usage, want_stream_options=False):
            yield tok, done
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


# Gemini's ListModels returns everything the key can reach, not just chat models.
# Two gates, in order of authority:
#
#   1. supported_actions — the API's own answer. A model without "generateContent"
#      cannot be prompted at all (embeddings, native-audio and live-translate models
#      speak embedContent / bidiGenerateContent instead). Dropping these needs no
#      maintenance and cannot go stale as Google ships new families.
#   2. Purpose — a model can accept generateContent and still be the wrong tool for a
#      chat box: TTS models want an audio response modality, image and robotics models
#      return something other than a reply. This gate is a name heuristic, so it stays
#      deliberately narrow; a model that slips through is merely odd, one wrongly
#      excluded is invisible.
_GEMINI_CHAT_ACTION = "generateContent"
_GEMINI_NON_CHAT = (
    "embedding", "-tts", "native-audio", "-image", "robotics", "computer-use",
    "-live-", "translate",
)


def _gemini_is_chat(model_id: str, actions) -> bool:
    """True when a Gemini model id is usable as a conversational model."""
    if _GEMINI_CHAT_ACTION not in set(actions or ()):
        return False
    return not any(tok in model_id for tok in _GEMINI_NON_CHAT)


async def gemini_models() -> list[dict]:
    """Fetch chat-capable Gemini models via the native SDK. Falls back to static list."""
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
            if not _gemini_is_chat(mid, getattr(m, "supported_actions", None)):
                continue
            name = next((x["name"] for x in GEMINI_MODELS_STATIC if x["id"] == mid), mid)
            ctx  = next((x["context"] for x in GEMINI_MODELS_STATIC if x["id"] == mid), 1048576)
            result.append({"id": mid, "name": name, "context": ctx,
                           "vision": supports_vision(mid)})
        return result if result else GEMINI_MODELS_STATIC
    except Exception as e:
        log.error(f"Gemini models error: {e}")
        return GEMINI_MODELS_STATIC


async def gemini_stream(messages: list, model: str, usage: dict | None = None):
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
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:   # each chunk carries a snapshot; the last one is the total
                _sink_usage(usage, getattr(um, "prompt_token_count", None),
                            getattr(um, "candidates_token_count", None))
            text = getattr(chunk, "text", None)
            if text:
                yield text, False
        yield "", True
    except Exception as e:
        log.warning(f"Gemini stream error: {e}")
        yield _gemini_err_msg(e), True


