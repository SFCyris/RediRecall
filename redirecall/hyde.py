# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.hyde — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
from . import providers

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HYDE — HYPOTHETICAL DOCUMENT EMBEDDINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def hyde_generate(query: str, provider: str, model: str) -> str:
    """
    Generate a short hypothetical document that would answer the query.

    HyDE (Hypothetical Document Embeddings) improves RAG retrieval by bridging
    the vocabulary gap between short queries and long documents:
      1. Ask the LLM to write a brief answer to the query.
      2. Embed the hypothetical answer (not the raw query).
      3. Use that embedding for KNN vector search.

    The hypothesis is never shown to the user — it is only used as the search
    vector.  The original query is still used for BM25 keyword search in hybrid mode.
    """
    prompt = [{"role": "user", "content": (
        "Write a concise 2-3 sentence factual passage that directly answers "
        "this question. Only write the passage, no preamble or explanation. "
        f"Question: {query}"
    )}]
    hypothesis = ""
    try:
        if provider == "claude":
            async for tok, done in providers.claude_stream(prompt, model):
                hypothesis += tok
                if done: break
        elif provider == "openai":
            async for tok, done in providers.openai_stream(prompt, model):
                hypothesis += tok
                if done: break
        elif provider == "qwen":
            async for tok, done in providers.qwen_stream(prompt, model):
                hypothesis += tok
                if done: break
        elif provider == "mistral":
            async for tok, done in providers.mistral_stream(prompt, model):
                hypothesis += tok
                if done: break
        elif provider == "groq":
            async for tok, done in providers.groq_stream(prompt, model):
                hypothesis += tok
                if done: break
        elif provider == "gemini":
            async for tok, done in providers.gemini_stream(prompt, model):
                hypothesis += tok
                if done: break
        else:
            async for tok, done in providers.ollama_stream(prompt, model):
                hypothesis += tok
                if done: break
    except Exception as e:
        log.warning(f"HyDE generation failed: {e}")
    return hypothesis.strip()

