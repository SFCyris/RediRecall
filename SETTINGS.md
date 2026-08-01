# RediRecall — Settings Reference

This document explains the main settings in the application: what each controls, its default value, acceptable range, and what changing it implies. A few advanced options (reranker, HyDE, scheduled re-crawl, session persistence) are configured in the UI but not covered in depth here.

---

## Table of Contents

1. [Redis Connection](#redis-connection)
2. [RAG Settings](#rag-settings)
3. [Semantic Cache Settings](#semantic-cache-settings)
4. [Provider Settings](#provider-settings)
5. [General / UI Settings](#general--ui-settings)
6. [Web Crawler Settings](#web-crawler-settings)
7. [Prompt Templates & Base Instruction](#prompt-templates--base-instruction)
8. [Security Settings](#security-settings)

---

## Redis Connection

Found in **Settings → Redis**.

### Host
- **Default:** `localhost`
- **What it does:** The hostname or IP address of your Redis server.
- **Notes:** Use `localhost` for a local installation. For Redis Enterprise or a remote server, use the full hostname (e.g. `redis-12345.mycloud.com`).

### Port
- **Default:** `6379`
- **What it does:** The TCP port Redis is listening on.
- **Notes:** `6379` is the generic default for an external Redis. RediRecall's own bundled local instance runs on `127.0.0.1:6389`. Redis Enterprise databases often use ports in the 10000–19999 range.

### Database Index
- **Default:** `0`
- **What it does:** Redis supports multiple logical databases (0–15) within a single server. Each has its own keyspace.
- **When to change:** Use a non-zero index to isolate this app from other software sharing the same Redis instance.
- **Notes:** Redis Enterprise does not support multiple databases per endpoint — leave this at `0`.

### Password
- **Default:** (empty)
- **What it does:** The `AUTH` password for the Redis server. RediRecall's bundled local instance runs on loopback with no password.
- **Notes:** Stored in `config.json`, which lives in the platform data directory (outside the repo), not the project tree. Only the Redis **host and port** can be overridden from the environment — `REDIRECALL_REDIS_HOST` / `REDIRECALL_REDIS_PORT` (used by the Docker image).

### SSL / TLS
- **Default:** Off
- **What it does:** Wraps the connection in TLS. Required for Redis Enterprise Cloud and most managed Redis services.
- **Notes:** When TLS is on, the server certificate **is** verified (redis-py's default), so a self-signed or untrusted certificate is rejected unless it chains to a trusted CA. Mutual TLS is not configurable from the UI.

### Additional Endpoints
Multiple Redis servers can be registered under custom names. Each RAG instance can then be assigned to a specific endpoint, allowing horizontal scaling across different Redis servers.

---

## RAG Settings

Found in **Settings → RAG**.

### Chunk Size
- **Default:** `180` words
- **Range:** 64 – 2048 words (practical) — but see the model limit below
- **What it does:** Controls the target size of each text chunk stored in the knowledge base. Text is split at sentence boundaries, so actual chunk sizes vary slightly. Content with no sentence punctuation (CSV rows, tables, code) is split on line boundaries instead, and no chunk may exceed twice this value.
- **⚠️ Bounded by the embedding model.** Each model encodes at most a fixed number of tokens — `intfloat/multilingual-e5-small` handles **256 tokens ≈ 190 English words** — and silently truncates the rest. Text past the limit is still stored and shown to the model, but is **not in the vector**, so semantic search cannot find it. Raising this above the model's limit therefore *reduces* recall while appearing to add context. RediRecall warns in the log when the configured size exceeds the active model's limit, and lowers a saved value that is already over it.
- **Smaller values (e.g. 128–256):**
  - More precise retrieval — each chunk covers a narrower topic
  - Higher storage requirements (more chunks)
  - Better for FAQ-style content or short factual passages
  - May lose context for answers requiring broader paragraphs
- **Larger values (e.g. 512–1024):**
  - More context per chunk — better for narrative or technical prose
  - Fewer chunks to store
  - May dilute the relevance score if the chunk covers multiple unrelated topics
- **Rule of thumb:** Leave at 180 for `intfloat/multilingual-e5-small`. If you switch to a model with a larger context (e.g. `all-mpnet-base-v2`, 384 tokens), you can raise it proportionally.

### Chunk Overlap
- **Default:** `32` words
- **Range:** 0 – (chunk_size / 2)
- **What it does:** The number of words at the end of one chunk that are repeated at the start of the next. This prevents answers from being cut off at chunk boundaries.
- **Example:** With chunk_size=180 and overlap=32, chunk 2 starts with the last ~32 words of chunk 1. A question whose answer spans a chunk boundary can still retrieve both halves.
- **0 overlap:** Maximum storage efficiency. Some answers near boundaries may be missed.
- **Large overlap (128+):** Better boundary coverage at the cost of more storage and potential duplicate content in search results.
- **Rule of thumb:** Keep at 32 unless you frequently see answers that seem truncated mid-sentence.

### Top-K
- **Default:** `5`
- **Range:** 1 – 20
- **What it does:** The maximum number of chunks retrieved from Redis and injected into the LLM prompt for each query.
- **Lower values (1–3):**
  - Shorter prompts → faster LLM responses, lower cost for API providers
  - Riskier: if the most relevant chunk is not in the top-1/3, the answer may be incomplete
- **Higher values (5–10):**
  - More context for the LLM → better answers for complex questions requiring multiple sources
  - Longer prompts → higher latency and API cost
  - Beyond 10, you risk filling the context window with marginally relevant content, potentially confusing the model
- **Rule of thumb:** 5 is a good default. Increase to 8–10 for research-style questions. Drop to 3 for fast, factual lookups.

### Similarity Threshold
- **Default:** `0.35`
- **Range:** 0.0 – 1.0
- **What it does:** The minimum cosine similarity score a chunk must achieve to be sent to the LLM. Chunks below this score are retrieved from Redis but discarded before prompt assembly. Chunks that matched the keyword (BM25) half of hybrid search are exempt — they were selected lexically, so a cosine bar is the wrong gate for them.
- **Cosine similarity scale — calibrate to your model.** The absolute numbers are much lower than intuition suggests. Measured with the default `intfloat/multilingual-e5-small` over a real documentation corpus, genuinely relevant question↔passage pairs score **0.35 – 0.75**, with a typical best match around **0.60**. Scores above 0.85 essentially mean near-duplicate text.
  - `0.75+` — near-duplicate; almost nothing clears this in practice
  - `0.50 – 0.70` — a normal good match
  - `0.35` — weak but often still useful (default)
  - `< 0.25` — probably noise
- **Too high (e.g. 0.90):**
  - Only very close paraphrases of indexed content will retrieve chunks
  - High miss rate — many questions return no RAG context
  - Use when you want the model to answer only from content that very closely matches the query
- **Too low (e.g. 0.50):**
  - Almost everything retrieves chunks
  - Risk of injecting irrelevant content into the prompt, degrading answer quality
  - May cause the model to "hallucinate" answers that blend relevant and irrelevant chunks
- **Diagnosing:** Check **Settings → Analytics → RAG Performance**. If *Avg Best Raw* is comfortably above the threshold but *Hit Rate* is low, the threshold is too high. A quick check: turn **Hybrid Search** off — if retrieval then returns nothing at all, the threshold is filtering out every vector match and hybrid search has been carrying retrieval on its own.

### Hybrid Search
- **Default:** On
- **What it does:** Runs both vector KNN search (semantic) and BM25 full-text search (keyword) simultaneously, then merges results using Reciprocal Rank Fusion (RRF).
- **With hybrid search on:**
  - Exact keywords are reliably found even when their embedding similarity is borderline
  - Paraphrases and conceptual matches are found even without exact word overlap
  - Best of both worlds: semantic + lexical retrieval
- **With hybrid search off:**
  - Only vector KNN is used
  - Exact keywords in the query may fail to retrieve chunks that use those keywords verbatim, if the cosine score is below threshold
  - Slightly faster
- **When to disable:** Almost never. Disable only if you are debugging or benchmarking vector-only retrieval.

---

## Semantic Cache Settings

Found in **Settings → Cache**.

### Enabled
- **Default:** On
- **What it does:** Toggles the entire semantic cache on or off.
- **When to disable:** During development or testing when you always want fresh LLM responses. When experimenting with different prompts or RAG configurations and do not want stale answers.

### Similarity Threshold
- **Default:** `0.92`
- **Range:** 0.0 – 1.0
- **What it does:** The minimum cosine similarity between the current query and a cached query for a cache hit to be returned.
- **Higher values (e.g. 0.97–0.99):**
  - Only near-identical questions return cached answers
  - Very conservative — low hit rate but high confidence that the cached answer is appropriate
  - Effectively behaves like exact matching at 0.99
- **Lower values (e.g. 0.80–0.85):**
  - Paraphrases and related questions trigger cache hits
  - Higher hit rate → lower LLM cost
  - Risk: semantically related but different questions may return an inappropriate cached answer
  - Example at 0.85: "What is Redis?" and "How does Redis work?" might share a cache entry
- **Rule of thumb:** 0.92 is a good balance. Lower to 0.88–0.90 if you want to aggressively cache paraphrases. Raise to 0.96+ if you notice wrong cached answers.

### TTL (Time to Live)
- **Default:** `3600` seconds (1 hour)
- **Range:** 60 – 86400+ seconds
- **What it does:** After this many seconds, a cache entry expires and is automatically deleted from Redis. Subsequent identical queries go to the LLM.
- **Short TTL (e.g. 300s):**
  - Cache entries expire quickly
  - Good for frequently changing data (news, live metrics)
  - Higher LLM usage
- **Long TTL (e.g. 86400s = 1 day):**
  - Entries persist longer — higher hit rate over time
  - Risk: stale answers if your underlying data changes
  - Good for stable reference documentation
- **TTL = 0:** Entries never expire (until cleared manually or Redis evicts them under memory pressure). Use only for truly static knowledge bases.

---

## Provider Settings

Found in **Settings → Providers** (accordion). Each provider card is expanded by clicking its header.

### Ollama

#### Host
- **Default:** `http://localhost`
- **What it does:** Base URL of the Ollama API server.
- **Notes:** Include the protocol (`http://` or `https://`). Do not include the port here — use the Port field.

#### Port
- **Default:** `11434`
- **What it does:** TCP port Ollama is listening on.
- **Notes:** Only change if you started Ollama with a custom `--port` flag.

#### Model
- **What it does:** The Ollama model name used for chat completions (e.g. `llama3.2`, `mistral`, `llava`).
- **Vision detection:** Models whose name contains `llava`, `bakllava`, `moondream`, `vision`, `minicpm`, `gemma3`, or `qwen-vl` are automatically detected as vision-capable and the 📎 image attach button is shown.

---

### Claude (Anthropic)

#### API Key
- **What it does:** Your Anthropic API key (starts with `sk-ant-`).
- **Security:** Prefer setting `ANTHROPIC_API_KEY` as an environment variable. If set this way, the field shows a placeholder and the key is never written to `config.json`.

#### Model
- **Default:** `claude-sonnet-4-6`
- **Available models:** `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`, and older versions.
- **Cost/capability tradeoff:**
  - Opus: highest capability, highest cost
  - Sonnet: balanced — best for most workloads
  - Haiku: fastest, lowest cost, lower capability

#### Base URL
- **Default:** `https://api.anthropic.com`
- **When to change:** If you use an Anthropic proxy, an on-premise gateway, or a third-party service that provides Claude access at a different endpoint.

---

### OpenAI

#### API Key
- **What it does:** Your OpenAI API key (starts with `sk-`).
- **Security:** Prefer `OPENAI_API_KEY` environment variable.

#### Model
- **Default:** `gpt-4o`
- **Notable options:** `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `o3`, `o4-mini`
- **o-series models:** Reasoning models (`o3`, `o4-mini`) do not support streaming in the same way. Use standard GPT-4o for best streaming experience.

#### Base URL
- **Default:** `https://api.openai.com`
- **When to change:** Point to any OpenAI-compatible API — Azure OpenAI, LM Studio, Together AI, local inference servers, etc.
- **Example values:**
  - `http://localhost:1234` — LM Studio
  - `https://api.together.xyz` — Together AI
  - `https://my-azure.openai.azure.com` — Azure (requires additional setup)

---

### Qwen (Alibaba DashScope)

#### API Key
- **What it does:** Your DashScope API key.
- **Security:** Prefer `DASHSCOPE_API_KEY` environment variable.

#### Model
- **Default:** `qwen-plus`
- **Options:** `qwen-plus`, `qwen-max`, `qwen-turbo`, `qwen-long`
- **Tradeoff:** `qwen-max` is most capable; `qwen-plus` (the default) balances quality and cost; `qwen-turbo` is fastest and cheapest; `qwen-long` supports very long contexts.

#### Base URL
- **Default:** `https://dashscope.aliyuncs.com/compatible-mode/v1`
- **Notes:** This URL already includes `/v1`. Do not modify unless DashScope changes their endpoint structure.

---

### Groq

#### API Key
- **What it does:** Your Groq API key (starts with `gsk_`).
- **Security:** Prefer `GROQ_API_KEY` environment variable.

#### Model
- **Default:** `llama-3.3-70b-versatile`
- **Options:** Various Llama 3.x, Mixtral, Gemma 2, and other open models hosted by Groq
- **Notes:** Groq's main selling point is inference speed — responses often arrive in under a second. The free tier has rate limits (RPM and TPM); if you hit them, wait a moment and retry.

#### Base URL
- **Default:** `https://api.groq.com/openai`
- **Notes:** The SDK appends `/v1` automatically. Only change if Groq updates their API endpoint.

---

### Gemini (Google AI)

#### API Key
- **What it does:** Your Google AI API key (starts with `AIza`).
- **Security:** Prefer `GEMINI_API_KEY` environment variable.

#### Model
- **Default:** `gemini-3-flash-preview`
- **Options:** the UI dropdown lists `gemini-3-flash-preview`, `gemini-2.5-flash-preview`, `gemini-2.5-pro-preview`, `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-1.5-pro`, `gemini-1.5-flash`
- **Tradeoffs:**
  - Flash models: fastest, good quality, best value
  - Pro models: highest capability and largest context, higher cost
  - Vision: current Gemini models support image input natively
- **Notes:** Uses the `google-genai` native SDK with async streaming.

---

## General / UI Settings

Found in **Settings → General**.

### Theme
- **Default:** `auto`
- **Options:** `auto`, `light`, `dark`
- **`auto`:** Follows the operating system's dark/light mode preference.

### Embedding Model
- **Default:** `intfloat/multilingual-e5-small`
- **Options:**

| Model | Dimensions | Speed | Quality | Use case |
|---|---|---|---|---|
| `intfloat/multilingual-e5-small` | 384 | Very fast | Good | General default |
| `all-mpnet-base-v2` | 768 | Moderate | Better | Higher accuracy |
| `paraphrase-multilingual-MiniLM-L12-v2` | 384 | Fast | Good | Non-English content |
| `BAAI/bge-base-en-v1.5` | 768 | Moderate | High | English, best quality |

- **Critical:** Changing the embedding model after indexing data **breaks all existing RAG indexes**. The vector dimensions change, so queries against old data will return nonsense. After changing the model, you must clear and re-ingest all RAG instances.
- **Larger models (768d):** Better retrieval quality at the cost of roughly 2× storage and slightly slower embedding at ingest time.

### Max Image Dimension
- **Default:** `1024` pixels
- **What it does:** Images attached to messages are resized (preserving aspect ratio) so the longest edge is at most this many pixels before being sent to the model.
- **Lower values (e.g. 512):** Smaller payloads, faster uploads, lower API cost. Use when image detail is not critical.
- **Higher values (e.g. 2048):** Preserves more detail — useful for reading text in images, detailed diagrams, or medical imagery.
- **Notes:** Most vision models do their own internal resizing. This setting primarily controls bandwidth and API payload size.

### Show RAG Matches in Answers
- **Default:** Off
- **What it does:** When on, the RAG chunk inspector expands automatically below every response that used RAG. When off, the inspector is collapsed — but still accessible by clicking the **📚 N chunks matched** badge.
- **When to enable:** During development or tuning, when you want to see retrieval quality on every response without manually clicking.
- **When to leave off:** Normal use — the badge is always visible; you can open it on demand.

---

## Web Crawler Settings

Found in **Settings → Web Sources** when configuring a URL crawl.

### Depth
- **Default:** `0` (page only)
- **Range:** 0 – 3
- **What it does:** How many link-hops to follow from the starting URL.
  - `0`: Only the starting URL itself is fetched. Single-page ingest.
  - `1`: The starting URL plus all links found on it.
  - `2`: Everything at depth 1, plus all links found on those pages.
  - `3+`: Exponential page count — use with Max Pages to avoid runaway crawls.
- **Rule of thumb:** Use depth 0–1 for specific pages, depth 1–2 for small sites, `llms.txt` manifests with depth 0 (the manifest handles link resolution internally).

### Max Pages
- **Default:** `0` (unlimited)
- **Range:** 0 – 500 (0 = unlimited)
- **What it does:** Caps the total number of pages fetched in a single crawl. The crawl stops as soon as this limit is reached, regardless of depth.
- **0 = unlimited:** Risky for large sites at depth 2+. Always set a cap when crawling the open web.
- **Practical values:** 50–200 pages covers most documentation sites; the Redis `llms.txt` preset typically fetches 80–150 pages.

### Respect robots.txt
- **Default:** On
- **What it does:** When on, the crawler reads the target site's `robots.txt` file and skips disallowed paths.
- **Leave on:** For external sites, so the crawler behaves as a good citizen and avoids private or admin paths.
- **When to turn off:** For your own internal sites whose `robots.txt` may block content you legitimately want to index.

### Local Links Only
- **Default:** On
- **What it does:** Restricts the crawler to only follow links within the same domain (e.g. if you start at `docs.example.com`, it won't follow links to `github.com`).
- **Note:** links listed in an `llms.txt` manifest are always followed regardless of this setting (manifests intentionally point across domains).

---

## Prompt Templates & Base Instruction

Found in **Settings → 💬 Templates**.

### Base Instruction

- **Config key:** `base_instruction`
- **Default:** the instruction shipped with this version (answer style, plus which visual block to use for charts, diagrams, maps, formulas and music)
- **What it does:** prepended to the system prompt of **every** chat turn, before any selected template. It is where global rules live — how answers should be formatted, and how to emit the fenced blocks that the app renders (see [DOCS.md](DOCS.md#rich-content-rendering)).
- **Leave it blank** to disable it entirely; the model then gets only the selected template (or a plain default).
- **↺ Reset to shipped default** replaces the box with the instruction that ships with the installed version. Use it after upgrading: once you have saved settings, your stored copy takes precedence over the shipped one, so newly supported block types would otherwise never be advertised to the model. You still have to click **Save Settings** afterwards.
- **Cost:** it is sent on every request, so its length counts toward input tokens on paid providers. Trim it if you do not need the visual blocks.

### Prompt Templates

- **Config key:** `prompt_templates` — a list of `{name, system}` objects
- Templates are **additive**: the effective system prompt is `base_instruction` + the selected template's text. Selecting one from the 💬 menu beside the message box does not replace the base.
- Use templates for personas or task-specific behaviour ("Redis expert", "ELI5"), and the Base Instruction for rules that should always apply.
- New templates start empty, so they only add to the base.

---

## Security Settings

Found in **Settings → Security**.

### Authentication

RediRecall has **no built-in authentication**. The Settings → Security "password" field is stored in `config.json` (as plaintext, in the platform data directory) but is **not currently enforced** — access is not gated on it.

By default the app binds to `127.0.0.1` (localhost only). Before exposing it on a LAN, VPN, or the internet, put it behind a **reverse proxy (nginx, Caddy) with HTTPS and authentication** — see [`deploy/docker-compose.https.yml`](deploy/docker-compose.https.yml) for a Caddy + automatic-HTTPS starting point, and add an auth layer there before exposing sensitive data.
