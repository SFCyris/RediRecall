# RediRecall — Documentation

> A self-hosted AI chat application with Retrieval-Augmented Generation (RAG) powered by Redis vector search, supporting Ollama, Claude, OpenAI, Qwen, Groq, and Gemini as LLM providers.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [Chat Interface](#chat-interface)
5. [RAG Instances](#rag-instances)
6. [LLM Providers](#llm-providers)
7. [Ingesting Content](#ingesting-content)
8. [Multi-RAG Parallel Queries](#multi-rag-parallel-queries)
9. [Semantic Cache](#semantic-cache)
10. [Multiple Redis Endpoints](#multiple-redis-endpoints)
11. [Settings Reference](#settings-reference)
12. [Keyboard Shortcuts](#keyboard-shortcuts)
13. [Analytics](#analytics)
14. [API Reference](#api-reference)
15. [Architecture](#architecture)
16. [Configuration File](#configuration-file)
17. [Optional Dependencies](#optional-dependencies)

---

## Overview

RediRecall is a single-server, self-hosted application that lets you:

- Chat with Ollama, Claude, OpenAI, Qwen, Groq, or Gemini
- Build named **RAG knowledge bases** from files, PDFs, web pages, and `llms.txt` manifests
- Store embeddings in **Redis** with HNSW vector indexing for fast semantic search
- Query **multiple RAG instances in parallel** across different Redis servers
- Cache repeated queries with a **semantic cache** that understands paraphrase similarity
- Render rich responses: Markdown and tables, syntax-highlighted code, LaTeX math (KaTeX), exact **function graphs** (math.js), **data charts** (Chart.js), **diagrams** (Mermaid, Graphviz), **geometric constructions** (JSXGraph), **maps** (Leaflet), **3-D plots** (Plotly), **molecules** (SmilesDrawer), **sheet music** (abcjs) and **inline SVG**
- Monitor retrieval quality per-instance with live analytics

<p align="center">
  <img src="screenshots/graph-and-formula.jpg" alt="LaTeX formulas, a comparison table, and a function graph rendered inline" width="49%">
  <img src="screenshots/notation-and-svg.jpg" alt="Sheet-music notation and an SVG geometry diagram rendered inline" width="49%">
</p>

---

## Quick Start

### Prerequisites

- Python 3.11+ (macOS also needs Homebrew + Xcode Command Line Tools)
- Redis 8 with the search/query engine — set up automatically by `install.sh` (or the `redis:8` service in Docker); no separate install needed
- At least one LLM backend: Ollama locally, or an API key for any supported cloud provider

### Install and run

```bash
./install.sh     # Python venv + a self-contained Redis 8 in ./.redis
./start.sh       # starts Redis, then the app
# → Open http://localhost:8420
```

Or with containers — pull the prebuilt multi-arch app image (from GitHub's Container Registry) plus Redis, then start:

```bash
docker compose pull      # ghcr.io/sfcyris/redirecall:latest + redis:8
docker compose up -d     # → http://localhost:8420
docker compose stop      # stop later (docker compose down to remove containers; data volume persists)
```

See the README for details.

On first launch a `config.json` is created (with defaults) in the per-platform data directory — `~/Library/Application Support/RediRecall` on macOS, `~/.local/share/redirecall` on Linux — not in the repo. Configure Redis and your LLM provider via **Settings** before chatting.

---

## Core Concepts

### RAG Instance

A named, isolated knowledge base. Each instance has its own Redis search index (`rag:{name}:idx`) containing vectorised text chunks. You can have as many instances as you like, each storing different domain knowledge (e.g. `product-docs`, `support-kb`, `internal-wiki`).

### Active RAG

The instance currently selected in the topbar dropdown. All queries are routed to this instance unless **parallel mode** (🔀) is enabled.

### Enabled / Disabled

Each instance can be toggled on or off without deleting it. Disabled instances are excluded from both single-instance and parallel queries.

| State | Single-instance mode | Parallel 🔀 mode |
|---|---|---|
| **Active** (selected in dropdown) | ✅ Searched | ✅ Searched |
| **Enabled** (not active) | ❌ Skipped | ✅ Searched |
| **Disabled** | ❌ Skipped | ❌ Skipped |

### Hybrid Search

Every RAG query runs two searches simultaneously and merges them using **Reciprocal Rank Fusion (RRF)**:

1. **Vector KNN** — semantic similarity via HNSW cosine distance. Finds paraphrases and related concepts.
2. **BM25 full-text** — keyword matching using Redis's built-in text index. Finds exact and near-exact terms even when their cosine score is borderline.

RRF formula: each result receives `1/(60 + rank)` for every list it appears in. Chunks ranking highly in both lists get the strongest boost.

Hybrid search is on by default and can be toggled in **Settings → RAG**.

### Semantic Cache

Before querying the LLM, every question is checked against a cache of previous (question, answer) pairs using cosine similarity. If a sufficiently similar question has been answered before, the cached response is returned instantly — no LLM call needed. RAG chunks from the original query are also stored in the cache and re-served with cache hits.

---

## Chat Interface

### Sending messages

Type in the input box at the bottom. Press **Enter** to send, **Shift+Enter** for a new line.

### Stopping a response

Click the red **■** stop button that appears during streaming to abort the current response. The partial text already generated is kept in the chat.

### Image attachments

Click the 📎 button or drag an image directly onto the input box. Images are sent to the model as multi-modal content. The vision badge (👁 Vision) appears in the topbar when the selected model supports images.

### Voice input

Click the 🎤 microphone button to toggle speech-to-text. Transcribed text is inserted into the input field.

### Prompt templates

Select a system-prompt template from the dropdown left of the input box to change how the model answers (e.g. "Redis Expert", "ELI5", or any custom template you've created).

### Message actions

Hover any AI message to reveal action buttons:
- 👍 / 👎 — Like or dislike (stored as feedback)
- 📌 — Pin the message to the pinned panel
- ↻ — Regenerate the response

### Cached message actions

Messages served from the semantic cache have two additional buttons:
- **🗑 Uncache** — Deletes this specific cache entry from Redis. Future identical queries will go to the LLM instead.
- **↺ Re-run fresh** — Re-sends the original query to the LLM, bypassing the cache. The new response automatically replaces the cached one.

### Rich content rendering

Answers are rendered inline: the model writes a short, declarative block and the browser draws it. The full set of supported types:

| Fence | Renders | You write | Engine |
|---|---|---|---|
| *(none)* | Markdown | Full GFM: headings, bold, tables, blockquotes, lists | marked |
| ` ```<language> ` | Code | Ordinary code | highlight.js |
| ` ```latex ` / ` ```math ` / `$…$` | Math formula | LaTeX | KaTeX |
| ` ```plot ` | **Function graph** (exact); optional live parameter sliders | `y = a*sin(b*x)`, `x = -5 .. 5`, `param: a = 0.5 .. 3 (1)` | math.js |
| ` ```chart ` | **Data chart** — bar, line, pie, doughnut, scatter, radar | Chart.js JSON | Chart.js |
| ` ```mermaid ` | **Diagram** — flowchart, sequence, class, state, ER, Gantt | mermaid syntax | Mermaid |
| ` ```dot ` | **Auto-laid-out graph** — dependency/call graphs | Graphviz DOT | Viz.js |
| ` ```geometry ` | **Geometric construction** | JSON: `boundingbox` + `elements` | JSXGraph |
| ` ```map ` | **Map** with markers / GeoJSON | JSON: `center`, `zoom`, `markers` | Leaflet + OpenStreetMap |
| ` ```plot3d ` | **3-D surface / scatter** | Plotly JSON | Plotly |
| ` ```molecule ` | **Chemical structure** | a SMILES string | SmilesDrawer |
| ` ```molecule3d ` | **3D structure**, rotatable/zoomable | XYZ format: atom count, comment line, then `Element x y z` per atom | 3Dmol.js |
| ` ```gantt ` | **Project schedule** — dates, durations, dependencies | mermaid gantt syntax (no leading `gantt` line) | mermaid |
| ` ```timeline ` | **Dated event sequence** | mermaid timeline syntax (no leading `timeline` line) | mermaid |
| ` ```network ` | **Force-directed graph**, draggable nodes | JSON `{"nodes":[…],"edges":[{"from":…,"to":…}]}` | vis-network |
| ` ```geojson ` | **Geographic features** with click popups | a GeoJSON FeatureCollection (coordinates are `[lng,lat]`) | Leaflet |
| ` ```abc ` | **Sheet music**, with a Play button | ABC notation | abcjs |
| ` ```calc ` | **Computed arithmetic** — units, dates, matrices | one expression per line, e.g. `5 km/h to m/s` | math.js |
| ` ```solve ` | **Symbolic algebra** — derivative, simplify, expand, roots, evaluate | `derivative: x^3 + 2x` | math.js |
| ` ```stats ` | **Descriptive statistics** + linear regression | `data: 4, 8, 15` (or `x:` / `y:` lines) | math.js |
| ` ```table ` | **Sortable / filterable table**, computed totals, CSV export | JSON: `columns`, `rows`, optional `total` | native |
| ` ```diff ` | **Line diff** (LCS) of two texts | `--- before` / lines / `--- after` / lines | native |
| ` ```regex ` | **Runs a pattern** against samples, highlights matches + groups | `pattern:` / `flags:` / `test:` lines | native |
| ` ```truth ` | **Truth table** for a boolean expression (≤10 vars) | `(A and B) or not C` | math.js |
| ` ```svg ` (or `xml` / raw `<svg>`) | Custom vector graphic | SVG markup (sanitised) | native |

Notes:

- Every rendered *figure* (chart, diagram, plot, map, molecule, score, SVG, LaTeX) carries a **Source** toggle and **Copy** button, so the underlying markup is always inspectable; SVG additionally offers **⬇ PNG**. Ordinary Markdown and code blocks are not figures — code blocks get a plain **Copy** button.
- A raw `<svg>…</svg>` with no code fence is detected and rendered too.
- **Computed, not asserted:** `calc`, `solve`, `stats`, `truth`, `table`, `diff` and `regex` are evaluated in your browser, not produced by the model. The model states *what* to compute (an expression, a data list, two texts); the browser returns the exact result — so unit conversions, column totals, derivatives and truth tables don't inherit the model's arithmetic mistakes. `calc`/`solve`/`stats`/`truth` use math.js (already loaded); `table`/`diff`/`regex` use no library at all.
- **Interactive `plot`:** declare a parameter with `param: a = <lo> .. <hi> (<init>)` and the block renders a slider; dragging it re-evaluates the formula and redraws instantly, with no round trip to the model.
- **Lazy loading:** the heavier renderers — Chart.js, Mermaid, Viz.js, JSXGraph, Leaflet, Plotly, SmilesDrawer, 3Dmol.js and highlight.js — are fetched only the first time a block of that type appears, so they cost nothing at page load. Markdown, sanitising, math and music (marked, DOMPurify, KaTeX, math.js, abcjs) load with the page because they are needed for ordinary answers.
- **Maximize and save as image:** every visual card (chart, diagram, plot, map, geometry, molecule, 3D molecule) has an ⛶ **Maximize** button that opens it full-viewport, and most also have a ⬇ **PNG** button. Not offered on `map` (no rasteriser wired up) or the plain-data lanes (`table`, `diff`, `regex`, `calc`, `solve`, `stats`, `truth`) — those aren't rasterised images.
- **Third-party requests:** all renderer libraries are served from a public CDN (cdnjs, plus jsDelivr for SmilesDrawer), so rendering is not fully offline. In addition, ` ```map ` fetches map tiles from `tile.openstreetmap.org` at view time — the only lane that sends *content-derived* data (the requested coordinates) to a third party. For an air-gapped deployment, vendor the libraries and serve them locally.
- **Safety:** SVG and diagram output is sanitised (DOMPurify) before insertion, and ` ```geometry ` accepts data only — never function or code strings.

### Rendering examples

| | |
|---|---|
| ![Mermaid diagram](screenshots/rendering/mermaid.png) | ![Data chart](screenshots/rendering/chart.png) |
| ![Graphviz graph](screenshots/rendering/dot.png) | ![Geometric construction](screenshots/rendering/geometry.png) |
| ![Map](screenshots/rendering/map.png) | ![3D surface plot](screenshots/rendering/plot3d.png) |
| ![Molecule](screenshots/rendering/molecule.png) | ![3D molecule](screenshots/rendering/molecule3d.png) |

| | |
|---|---|
| ![Timeline](screenshots/rendering/timeline.png) | ![Gantt](screenshots/rendering/gantt.png) |
| ![Network](screenshots/rendering/network.png) | ![GeoJSON](screenshots/rendering/geojson.png) |

### Working with tables and charts

Every Markdown table an answer produces gets a toolbar: click any header to sort
(numbers, currency and dates sort by value, not alphabetically), filter rows as
you type, and copy or download the visible rows as CSV. The header stays put
while the body scrolls.

![Sortable table](screenshots/rendering/table-sort.png)

Line, scatter and bubble charts support wheel-zoom (hold Ctrl) and drag-to-pan,
with a **Reset zoom** button once you have moved. Every chart also has a **Data**
button that reveals the series behind it as one of those sortable tables — so you
can check the numbers a chart was drawn from.

![Chart data view](screenshots/rendering/chart-data.png)


### RAG context inspector

After each AI response that used RAG, a **📚 N chunks matched** badge appears in the message metadata row.

- By default the inspector is **collapsed** — click the badge to expand it
- To open it automatically after every answer, enable **Show RAG Matches in Answers** in **Settings → General**
- Cache hits also show their associated RAG chunks (stored at cache-write time)

### Citations

When an answer is grounded in your knowledge base, the retrieved passages are numbered and the model marks each claim with the passage it came from — `[1]`, `[2]`, and so on. The numbers correspond to the entries in the RAG context inspector, so any individual sentence can be traced back to the chunk that supports it.

<p align="center">
  <img src="screenshots/rendering/citations-scope.png" alt="An answer with [1] and [2] citation markers, the matched-chunk inspector, and a source-scope chip above the composer" width="88%">
</p>

### Scoping a question to one document

Click **⌖ only this** on any chunk in the inspector — or the ⌖ button in the document list — to restrict the *next* question to that source. A chip appears above the composer showing the active scope; click ✕ to clear it. The filter is applied inside the search itself (both the vector and keyword halves), not by discarding results afterwards, so scoped questions still return the best matches *within* that document.

### When nothing matches

If retrieval runs and finds nothing above the similarity threshold, the answer is marked **📭 No KB match** and the model states plainly that it is answering from general knowledge rather than your documents. Click the badge to open the RAG threshold setting.

<p align="center">
  <img src="screenshots/rendering/no-kb-match.png" alt="An answer marked with a No KB match badge" width="88%">
</p>

An answer with no badge at all was produced without RAG (all instances disabled, or none selected).

### Session management

Sessions are listed in the left sidebar. Click any session to switch to it, or the ✕ to delete it (with confirmation).

Conversations are **persisted in Redis and restored on reload** — closing the tab or restarting the browser no longer loses them, and the conversation you were last in is reopened automatically. The sidebar lists conversations started in *this* browser; because RediRecall has no user accounts, it deliberately does not list sessions created elsewhere.

Each message records the provider and model that produced it. Sessions are automatically titled from the first message.

### Regenerating an answer

**↻ Regenerate** re-asks the question that *that* answer belongs to and replaces it **in place**, rather than appending a second copy of the question. Earlier attempts are kept: a **‹ 1/2 ›** control appears in the message metadata row so versions can be compared. Regenerating an answer that has later turns after it will discard those turns, and asks for confirmation first.

---

## RAG Instances

### Creating an instance

1. Open **Settings → RAG**
2. Click **＋ New Instance**
3. Enter a name (letters, numbers, hyphens), pick a colour, and optionally select a Redis endpoint
4. Click **Create**

### Instance controls

Each card in the instance list exposes:

| Button | Action |
|---|---|
| **● On / ○ Off** | Enable or disable the instance |
| **📄 Documents** | Browse the documents in the instance; scope a question to one, or delete one |
| **⬇** | Export the instance to a `.zip` file (includes all chunks and embeddings) |
| **⬆** | Import from a previously exported `.zip` |
| **✦ Dedupe** | Deduplicate — remove exact-duplicate chunks to reduce storage footprint |
| **🧹** | Clear all chunks (wipes data, keeps the instance) |
| **🗑** | Permanently delete the instance |

### Managing individual documents

**📄 Documents** lists every source in the instance with its chunk count and ingest date.

<p align="center">
  <img src="screenshots/rendering/documents.png" alt="The document manager listing each source URL with its chunk count, a scope button and a delete button" width="88%">
</p>

- **⌖** scopes the next question to that document (see *Scoping a question to one document*)
- **🗑** removes just that document's chunks, leaving the rest of the instance untouched

Deleting a document also releases its de-duplication fingerprints, so the same file or URL can be re-ingested afterwards — which is the supported way to refresh a single stale document without wiping and rebuilding the whole instance.

The date column shows `—` for documents ingested before this metadata was recorded; they are otherwise fully functional and gain a date when re-ingested.

### Exporting and importing

Exports produce a ZIP containing `meta.json` and `chunks.json`. Embeddings are included as base64, so re-embedding is not needed on import — making it fast.

When importing into an existing instance, the current endpoint assignment and colour are preserved; only the chunk data is replaced.

### Deduplication (Chunk Optimization)

Click **✦ Dedupe** on any instance card to remove exact-duplicate chunks. Two chunks are considered duplicates if their text is identical after lowercasing and whitespace normalisation.

---

## LLM Providers

All providers are configured in **Settings → Providers** — a unified accordion UI where each provider is a collapsible card. Click any card header to expand it; the currently active provider is highlighted.

### Provider overview

| Provider | Type | Notes |
|---|---|---|
| ⚡ Ollama | Local | Any model installed locally; vision auto-detected |
| ✦ Claude | API | Anthropic native SDK; `claude-opus-4-6`, `claude-sonnet-4-6`, etc. |
| ◆ OpenAI | API | OpenAI native SDK; GPT-4o, o-series |
| 🟣 Qwen | API | Alibaba DashScope (OpenAI-compatible), `qwen-max`, `qwen-plus`, etc. |
| ⚡ Groq | API | Ultra-fast inference (OpenAI-compatible), Llama / Mixtral / Gemma |
| ✦ Gemini | API | Google AI native SDK; `gemini-2.0-flash`, `gemini-1.5-pro`, etc. |

### Switching providers

Click the **Use** button inside any provider card, or use the provider selector in the topbar.

### Environment variables

API keys can be supplied via environment variables — they are never written to `config.json`:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DASHSCOPE_API_KEY="sk-..."
export GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY="AIza..."
```

### OpenAI-compatible endpoints

Any provider using the OpenAI-compatible API (OpenAI, Qwen, Groq, and many others) can be pointed at a custom base URL.

| Provider | Default base URL |
|---|---|
| OpenAI | `https://api.openai.com` |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Groq | `https://api.groq.com/openai` |
| LM Studio | `http://localhost:1234` |
| Together AI | `https://api.together.xyz` |

### Native SDK usage

Each provider uses its official Python SDK for streaming:

| Provider | SDK | Streaming method |
|---|---|---|
| Claude | `anthropic` | `AsyncAnthropic.messages.stream()` |
| OpenAI | `openai` | `AsyncOpenAI.chat.completions.create(stream=True)` |
| Qwen | `openai` (DashScope) | Same as OpenAI with custom `base_url` |
| Groq | `openai` (Groq endpoint) | Same as OpenAI with custom `base_url` |
| Gemini | `google-genai` | `client.aio.models.generate_content_stream()` |

---

## Ingesting Content

### From files — with live progress

1. **Settings → RAG → Ingest Documents**
2. Select a target RAG instance
3. Drop files onto the upload area or click to browse
4. Supported formats: `.txt`, `.md`, `.csv`, `.pdf` (needs PyMuPDF), `.docx` (needs python-docx), `.xlsx` (needs openpyxl)

Files are processed one by one via a streaming SSE connection. The progress bar advances per-file and shows the file name and chunk count in real time.

### From a URL

1. **Settings → Web Sources**
2. Enter a URL and configure:

| Setting | Description |
|---|---|
| **Depth** | 0 = page only; 1–3 = follow links N levels deep |
| **Max Pages** | 0 = unlimited; set a cap to avoid runaway crawls |
| **Respect robots.txt** | Honour crawl restrictions |
| **Local links only** | Stay within the same domain |

3. Click **🕷 Start Crawl** — progress streams in real time

### llms.txt manifests

URLs ending in `llms.txt` (e.g. `https://redis.io/llms.txt`) are treated as curated link manifests. All linked pages are fetched regardless of domain. This is the fastest way to ingest entire documentation sites.

**Presets** for common sources:
- 🟥 Redis Docs (llms.txt)
- 🐍 Python Docs
- 🦜 LangChain

### Chunking — sentence-aware

Text is split into overlapping chunks that **respect sentence boundaries**. The chunker splits on `.`, `!`, and `?` first, then groups sentences into windows of approximately `chunk_size` words.

Content with no sentence punctuation — CSV and spreadsheet rows, Markdown tables, code — is split on line boundaries instead, so a large table cannot become one enormous chunk. A chunk is never allowed to exceed twice `chunk_size`.

Configure in **Settings → RAG**:

| Setting | Default | Description |
|---|---|---|
| **Chunk Size** | 180 words | Target words per chunk |
| **Chunk Overlap** | 32 words | Words of context carried into the next chunk |

> **Chunk size is bounded by the embedding model.** Each model can only encode a fixed number of tokens (`intfloat/multilingual-e5-small` handles 256, roughly 190 English words) and silently truncates anything longer — the excess text is still stored and shown, but is **not represented in the vector**, so it cannot be found by semantic search. RediRecall warns in the log when the configured chunk size exceeds what the active model can encode, and lowers a saved value that is already over the limit (logging the change).

---

## Multi-RAG Parallel Queries

Click the **🔀** button in the topbar (next to the RAG dropdown) to enter parallel mode.

In parallel mode:
- **All enabled** RAG instances are queried simultaneously
- Each instance can live on a **different Redis server** — queries fan out concurrently
- Results from all instances are merged and re-ranked by score
- The top-K chunks (across all knowledge bases) are injected into the LLM context
- Each chunk in the RAG inspector shows which instance it came from (🗄 instance-name)

To return to single-instance mode, click 🔀 again or select a specific instance from the dropdown.

---

## Semantic Cache

The cache intercepts queries before they reach the LLM. If a semantically equivalent question exists in the cache (above the similarity threshold), the stored answer — including associated RAG chunks — is returned immediately.

| Setting | Default | Effect |
|---|---|---|
| **Enabled** | ✅ | Toggle the cache on/off |
| **Similarity Threshold** | 0.92 | Higher = stricter match required for a cache hit |
| **TTL** | 3600 s | Seconds before a cached entry expires |

Cache hits appear with a green **⚡ Cached XX%** badge showing the match score.

Cache misses appear with a yellow **🔍 Live** badge.

### Managing individual cache entries

Every cached message has two action buttons:

- **🗑 Uncache** — Removes just that one cache entry from Redis. Future queries that would have matched it will go to the LLM instead.
- **↺ Re-run fresh** — Bypasses the cache for this query only, re-generates a fresh response, and the new answer automatically updates the cache.

Clear the entire cache in **Settings → Cache → 🗑 Clear Cache**.

> **Note:** The semantic cache requires Redis Stack (RediSearch module). If your Redis instance does not have the Search module, the cache is silently disabled and a single warning is logged on startup.

---

## Multiple Redis Endpoints

Each RAG instance can be stored on a different Redis server. This allows horizontal scaling across Redis Enterprise databases.

### Adding an endpoint

1. **Settings → Redis → Additional Redis Endpoints**
2. Click **＋ Add Endpoint**
3. Fill in name, host, port, password, database index, and SSL toggle
4. Click **Add**

### Assigning an instance to an endpoint

When creating a new RAG instance, the **Redis Endpoint** dropdown lists all configured endpoints.

### How routing works

- `rc_for_instance(name)` searches **all configured endpoints** for the instance's metadata key (`rag_meta:{name}`)
- Queries, ingestion, exports, and deduplication are automatically routed to the correct server
- Parallel 🔀 mode fans out to multiple servers concurrently using `asyncio.gather`

---

## Settings Reference

Settings are organised into tabbed groups. See [SETTINGS.md](SETTINGS.md) for a detailed explanation of every individual field and its implications.

### Group 1 — Infrastructure

#### 💡 Status
Live health check for all connected services. Shows Redis version, Ollama availability, and API key validity. Local checks (Redis, Ollama) run every 30 seconds; cloud provider key checks run every 5 minutes.

#### 🗄 Redis
Configure the primary Redis connection (host, port, db, password, SSL). Manage additional named endpoints. Memory usage monitor.

### Group 2 — Knowledge

#### 📚 RAG
- **Chunk Size** and **Chunk Overlap** (sentence-aware chunking)
- **Top-K** — number of chunks returned per query
- **Similarity Threshold** — minimum cosine score for a chunk to be sent to the LLM
- **⚡ Hybrid Search** — combines vector KNN with BM25 full-text search (recommended: on)
- RAG instance management: create, delete, toggle, export, import, deduplicate
- File ingestion panel with live per-file streaming progress

#### 🌐 Web Sources
URL crawler with depth and page-cap controls. Real-time progress log. Saved web source list.

#### ⚡ Cache
Enable/disable semantic cache, threshold, TTL, analytics, and clear button.

### Group 3 — Providers

#### 🤖 Providers
All six providers in a single accordion tab. Each provider has a collapsible card showing its configuration fields, connection test button, and a **Use** button to set it as the active provider.

The card header shows:
- A status dot (green = connected, red = error, grey = not configured)
- Provider name and category tag (Local / Free / API)
- Currently selected model name
- Expand/collapse chevron

### Group 4 — App Config

#### 🔧 General
- **Theme** — light / dark / auto
- **Embedding Model** — select and re-index if changed
- **Max Image Dimension** — for vision model attachments
- **📚 Show RAG Matches in Answers** — auto-expand RAG inspector
- Config import/export/reset

#### 💬 Templates
Create, edit, and delete named system prompts.

#### 🔐 Security
RediRecall has **no built-in authentication**. The password field here is stored but **not currently enforced** — access is not gated on it. The app binds to `127.0.0.1` by default; put a reverse proxy with HTTPS + auth in front before exposing it to a network (see `deploy/docker-compose.https.yml`).

### Group 5 — Diagnostics

#### 📊 Analytics
Session-level cache hit/miss rates, latency histogram, message statistics, and per-instance RAG performance table.

#### 📋 Logs
Last 200 ingestion events. Filter by status, instance, or source URL.

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Enter` | Send message |
| `Shift+Enter` | New line in input |
| `⌘/Ctrl+K` | Open Settings |
| `⌘/Ctrl+F` | Search chat |
| `Esc` | Close panel |

---

## Analytics

Open **Settings → Analytics** for live performance statistics.

### Session-level metrics

- **Total Queries** — messages sent this session
- **Cache Hits** — how many were served from the semantic cache
- **Hit Rate** — cache hits / total queries
- **Avg Latency** — mean end-to-end response time
- **Latency Distribution** — histogram across five buckets: <200ms, 200–500ms, 500ms–1s, 1–3s, >3s

### RAG Performance — per instance

| Column | Description |
|---|---|
| **Queries** | Total searches run against this instance |
| **Hits** | Queries that returned ≥1 chunk above the similarity threshold |
| **Hit Rate** | Hits / Queries — colour-coded (green ≥80%, yellow ≥50%, red <50%) |
| **Avg Score (hits)** | Mean cosine similarity of the top-1 chunk on hit queries |
| **Avg Best Raw** | Mean cosine similarity of the top-1 KNN result before threshold filtering |
| **Avg Chunks** | Mean number of chunks returned per query |

#### Diagnosing low hit rate

If **Avg Best Raw** is high (e.g. 0.65+) but **Hit Rate** is low, your `similarity_threshold` is too strict. Lower the threshold in **Settings → RAG**.

What counts as "strict" depends on the embedding model, and the useful range is lower than it looks. With `intfloat/multilingual-e5-small`, genuinely relevant question↔passage pairs typically score **0.35–0.75**; a threshold of 0.75 clears almost nothing. If the vector half of hybrid search appears to contribute little — or turning `hybrid_search` off returns nothing at all — the threshold is the first thing to lower.

#### Reranking

With **Settings → RAG → Reranker** enabled, a cross-encoder re-scores the retrieved candidates before they reach the model. Because a reranker can only improve on the original ordering if it is given more candidates than you intend to keep, RediRecall widens retrieval to `rerank_candidates` (default 40) whenever reranking is on, then cuts back to `top_k` afterwards. With the reranker off, retrieval fetches only `top_k` and no extra work is done.

A **⚠ threshold?** warning pill appears automatically on any instance where raw score ≥ 0.60 but hit rate < 50%.

---

## API Reference

All endpoints are served at `http://localhost:8420`.

### Config

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Fetch runtime configuration |
| `POST` | `/api/config` | Save configuration |
| `GET` | `/api/config/export` | Download config as JSON file |
| `POST` | `/api/config/import` | Upload and replace config |

### Status

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status/redis` | Ping primary Redis |
| `GET` | `/api/status/redis/{endpoint}` | Ping named endpoint |
| `GET` | `/api/status/ollama` | Test Ollama server |
| `GET` | `/api/status/claude` | Verify Claude API key |
| `GET` | `/api/status/openai` | Verify OpenAI API key |
| `GET` | `/api/status/qwen` | Verify Qwen API key |
| `GET` | `/api/status/groq` | Verify Groq API key |
| `GET` | `/api/status/gemini` | Verify Gemini API key |

### Models

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/ollama/models` | List Ollama models (vision-detected) |
| `GET` | `/api/claude/models` | List Claude models |
| `GET` | `/api/openai/models` | List OpenAI models (live + static fallback) |
| `GET` | `/api/qwen/models` | List Qwen models |
| `GET` | `/api/groq/models` | List Groq models (live + static fallback) |
| `GET` | `/api/gemini/models` | List Gemini models |

### Redis Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/redis/endpoints` | List all endpoints |
| `POST` | `/api/redis/endpoints` | Add endpoint |
| `DELETE` | `/api/redis/endpoints/{name}` | Remove endpoint |
| `POST` | `/api/redis/endpoints/{name}/test` | Test connectivity |
| `GET` | `/api/redis/memory` | Memory usage stats |

### RAG Instances

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/instances` | List all instances across all endpoints |
| `POST` | `/api/rag/instances` | Create instance (`{name, color, redis_endpoint}`) |
| `DELETE` | `/api/rag/instances/{instance}` | Delete instance and all chunks |
| `POST` | `/api/rag/{instance}/toggle` | Enable / disable |
| `POST` | `/api/rag/{instance}/reset` | Clear chunks, keep instance |
| `POST` | `/api/rag/{instance}/optimize` | Remove exact-duplicate chunks |
| `GET` | `/api/rag/{instance}/chunks` | Preview stored chunks |

### Ingestion

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/rag/{instance}/ingest/files` | Upload and ingest files (returns JSON array) |
| `POST` | `/api/rag/{instance}/ingest/files/stream` | Upload and ingest files with SSE progress |
| `POST` | `/api/rag/{instance}/ingest/url` | Crawl URL (non-streaming) |
| `GET` | `/api/rag/{instance}/ingest/url/stream` | Crawl with SSE progress |
| `GET` | `/api/rag/logs` | Last 200 ingestion events |

#### SSE file ingest events

```jsonc
// Per-file progress
{ "file": "manual.pdf", "status": "ok", "chunks": 128, "index": 0, "total": 3 }
{ "file": "faq.txt",    "status": "ok", "chunks": 42,  "index": 1, "total": 3 }
{ "file": "broken.csv", "status": "error", "error": "...", "index": 2, "total": 3 }

// Completion sentinel
{ "done": true, "total": 3 }
```

### Export / Import

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/{instance}/export` | Download ZIP (chunks + embeddings) |
| `POST` | `/api/rag/{instance}/import` | Upload ZIP into instance |

### Cache

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/cache/stats` | Hit count, entry count |
| `DELETE` | `/api/cache` | Clear all cached responses |
| `DELETE` | `/api/cache/entry?entry_id={id}` | Delete a single cache entry by ID |

**`DELETE /api/cache/entry`** — removes a specific cached response without touching any other entries. The `entry_id` is included in every `cache_hit` WebSocket message and stored as `data-entry-id` on cached message elements.

```jsonc
// Request
DELETE /api/cache/entry?entry_id=abc123

// Response
{ "ok": true }
// or on failure:
{ "ok": false, "error": "entry_id required" }
```

### RAG Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/stats` | Per-instance query statistics |
| `DELETE` | `/api/rag/stats` | Reset all counters |

### Sessions

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/sessions` | List sessions |
| `GET` | `/api/sessions/{sid}` | Fetch message history |
| `DELETE` | `/api/sessions/{sid}` | Delete session |

### Templates

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/templates` | List templates |
| `POST` | `/api/templates` | Save templates |

### WebSocket

```
WS /ws/chat/{session_id}
```

**Client → Server messages:**

```jsonc
// Start a chat turn
{
  "content": "What is Redis?",
  "provider": "ollama",             // "ollama" | "claude" | "openai" | "qwen" | "groq" | "gemini"
  "model": "llama3.2",
  "system_prompt": "You are a helpful assistant.",
  "rag_instance": "product-docs",   // single instance
  // OR
  "rag_instances": ["docs", "kb"],  // parallel multi-instance
  "images": [],                      // base64 data URIs for vision
  "bypass_cache": false              // true = skip cache lookup for this query
}

// Abort current stream
{ "type": "abort" }
```

**Server → Client messages:**

```jsonc
{ "type": "stream_start" }
{ "type": "rag_context", "chunks": [...], "latency": { "rag": 0.12 } }
{ "type": "token", "content": "Hello", "done": false }
{ "type": "stream_end", "latency": { "cache": 0, "rag": 0.12, "llm": 1.4, "total": 1.52 }, "title": "Redis Overview" }
{
  "type": "cache_hit",
  "content": "...",
  "score": 0.97,
  "entry_id": "semcache:abc123",    // used for targeted deletion
  "latency": { "cache": 0.03, "total": 0.03 }
}
{ "type": "error", "content": "Connection refused" }
{ "type": "stream_end", "aborted": true, "latency": {} }
```

`rag_context` is sent after `cache_hit` when the cache entry contains stored RAG chunks, allowing the client to attach them to the cached message's inspector panel.

`bypass_cache: true` causes the server to skip the cache lookup for that single query. The resulting response is still written to the cache at the end.

---

## Architecture

```
Browser (index.html)
  │
  ├── WebSocket /ws/chat/{sid}  ←→  handle_chat()
  │     ├── Semantic cache lookup   (Redis FLAT index, KNN)
  │     │     └── cache_hit → re-emit stored RAG chunks
  │     ├── RAG retrieval           (Redis HNSW index, KNN + BM25 → RRF merge)
  │     │     └── search_rag_parallel() — asyncio.gather across endpoints
  │     └── LLM streaming           (Ollama / Claude / OpenAI / Qwen / Groq / Gemini)
  │
  └── REST /api/*  ←→  FastAPI routes
        ├── Config CRUD
        ├── RAG instance management (create / toggle / reset / optimize)
        ├── File & URL ingestion    (batch + SSE streaming)
        ├── Export / Import
        ├── Cache management        (stats / clear all / delete single entry)
        └── Analytics (cache stats, RAG per-instance stats)

Redis (one or more servers)
  ├── rag:{instance}:idx           — HNSW vector index + TEXT field for BM25
  ├── rag:{instance}:chunk:{id}    — HASH: text, source, embedding
  ├── rag:{instance}:chunk_counter — INCRBY atomic ID counter
  ├── rag_meta:{instance}          — JSON metadata (color, endpoint, enabled)
  ├── semcache:idx                 — FLAT vector index for response cache
  └── semcache:{id}                — HASH: query, response, embedding, chunks_json, TTL
```

### Vector indexes

| Index | Type | Use case |
|---|---|---|
| `rag:{instance}:idx` | HNSW (M=16, EF_CONSTRUCTION=200) | RAG chunk retrieval — scales to ~1M chunks |
| `semcache:idx` | FLAT (brute-force) | Cache lookup — perfect recall on small sets |

### Cache metadata storage

Each cache entry (`semcache:{id}` HASH) stores:

| Field | Content |
|---|---|
| `query` | Original query text |
| `response` | Full LLM response |
| `embedding` | Query vector (for similarity search) |
| `chunks_json` | JSON-encoded RAG chunks retrieved during the original query |

When a cache hit occurs, `chunks_json` is decoded and re-emitted as a `rag_context` WebSocket message, so the client renders the same "See also" inspector panel as the original response.

### Hybrid search — RRF detail

```
query
  ├── embed(query) → KNN search  → vec_rows  [rank 0..N]
  └── keywords(query) → @text:() → bm25_rows [rank 0..N]

for each result key:
    rrf_score += 1 / (60 + rank)   ← applied separately per list

ranked by rrf_score desc → top_k returned
threshold filter applied to cosine score (not RRF score)
```

### Streaming abort

The WebSocket handler uses `asyncio.create_task` so the server can concurrently receive an `abort` message while streaming tokens. Cancelling the task raises `CancelledError` at the next `await`, cleanly stopping the LLM generator.

### Embedding model

Default: `intfloat/multilingual-e5-small` (384 dimensions, fast). Change in **Settings → General**.

> **Important:** Changing the embedding model requires re-indexing all existing RAG data — the vector dimensions will not match.

Available models:
- `intfloat/multilingual-e5-small` — fast, 384d
- `all-mpnet-base-v2` — more accurate, 768d
- `paraphrase-multilingual-MiniLM-L12-v2` — multilingual
- `BAAI/bge-base-en-v1.5` — high quality, English

Embeddings are computed locally by `sentence-transformers` (PyTorch). On the local install the device is whatever your PyTorch build supports — Apple Silicon uses `mps`, a CUDA machine uses the GPU.

**In Docker, embeddings run on CPU.** The image installs PyTorch from the CPU-only index on purpose: the default PyPI `torch` pulls NVIDIA CUDA runtime wheels that are proprietary (`LicenseRef-NVIDIA-SOFTWARE-LICENSE`), and redistributing those inside an AGPL-3.0 image would combine proprietary binaries with copyleft code. It also removes about 2 GB from the image. See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

To use an NVIDIA GPU, install the CUDA build **on your own machine** rather than expecting it in the published image — nothing proprietary is then distributed by this project:

```bash
# pick the channel matching your driver: cu126 | cu128 | cu129 | cu130
docker compose exec redirecall pip install --force-reinstall torch \
  --index-url https://download.pytorch.org/whl/cu129
```

The container also needs GPU access (`--gpus all`, or a `deploy.resources` reservation in compose), and the step must be repeated after every `docker compose pull` because a fresh image is CPU-only again. For a permanent GPU image, change the `pip install` line in the `Dockerfile` and build it yourself.

---

## Configuration File

`config.json` (in the platform data directory — see the README) is created on first run and updated via the Settings UI or the `/api/config` endpoint.

```jsonc
{
  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": "",
    "ssl": false
  },
  "redis_endpoints": [],
  "provider": "ollama",
  "ollama": { "host": "http://localhost", "port": 11434, "model": "llama3.2" },
  "claude": {
    "api_key": "",
    "model": "claude-sonnet-4-6",
    "base_url": "https://api.anthropic.com"
  },
  "openai": {
    "api_key": "",
    "model": "gpt-4o",
    "base_url": "https://api.openai.com"
  },
  "qwen": {
    "api_key": "",
    "model": "qwen-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
  },
  "groq": {
    "api_key": "",
    "model": "llama-3.3-70b-versatile",
    "base_url": "https://api.groq.com/openai"
  },
  "gemini": {
    "api_key": "",
    "model": "gemini-3-flash-preview"
  },
  "embedding": { "model": "intfloat/multilingual-e5-small", "max_image_dim": 1024 },
  "rag": {
    "chunk_size": 180,
    "chunk_overlap": 32,
    "top_k": 5,
    "similarity_threshold": 0.35,
    "hybrid_search": true,
    "rerank_candidates": 40
  },
  "cache": { "enabled": true, "similarity_threshold": 0.92, "ttl": 3600 },
  "ui": {
    "theme": "auto",
    "show_rag_matches": false
  },
  "active_rag": "default",
  "base_instruction": "…prepended to every chat turn; blank = use the shipped default…",
  "prompt_templates": [
    { "name": "Default",      "system": "" },
    { "name": "Redis Expert", "system": "You are an expert in Redis..." }
  ],
  "web_sources": [],
  "security": { "enabled": false, "password": "" }
}
```

---

## Backup and Restore

Two things hold state, and they are separate:

| What | Where | Contains |
|---|---|---|
| Redis | the `redirecall-redis` volume (`/data` in that container) | every chunk, vector and index; sessions; the semantic cache |
| App data | the `redirecall-data` volume, or `$REDIRECALL_DATA_DIR` | `config.json`, uploads, ingestion logs, feedback |

A backup needs both. Restoring only Redis leaves the app without API keys or
endpoints; restoring only the app data leaves it with no documents.

### Docker

Redis writes an append-only file, so snapshot it after asking for a rewrite:

```bash
docker compose exec redis redis-cli BGREWRITEAOF
docker run --rm -v redirecall-redis:/src -v "$PWD":/out alpine \
  tar czf /out/redis-backup.tgz -C /src .
docker run --rm -v redirecall-data:/src -v "$PWD":/out alpine \
  tar czf /out/appdata-backup.tgz -C /src .
```

Restore into a stopped stack:

```bash
docker compose down
docker run --rm -v redirecall-redis:/dst -v "$PWD":/in alpine \
  sh -c "rm -rf /dst/* && tar xzf /in/redis-backup.tgz -C /dst"
docker run --rm -v redirecall-data:/dst -v "$PWD":/in alpine \
  sh -c "rm -rf /dst/* && tar xzf /in/appdata-backup.tgz -C /dst"
docker compose up -d
```

### Local install

```bash
redis-cli -p 6390 BGREWRITEAOF
tar czf redis-backup.tgz -C /path/to/redis/dir .
tar czf appdata-backup.tgz -C "$REDIRECALL_DATA_DIR" .
```

### Verifying a restore

`GET /api/health` returns `status: ok`, and `GET /api/rag/instances` lists each
instance with a non-zero chunk count. If chunk counts are zero while the keys
exist, the index did not rebuild — restart once and check the log for the schema
version line.

### What a backup does not protect

Chunks are stored with the vector produced by the embedding model configured at
ingest time. Restoring a backup onto an install with a different embedding model
leaves the vectors intact but unsearchable by the new model; RediRecall logs a
warning when it detects this. Re-ingest after changing models.

## Optional Dependencies

Most of the packages below are **installed automatically** by `install.sh` and the Docker image (they're declared in `pyproject.toml`) — PDF/DOCX/XLSX extraction, web-content extraction, and the provider SDKs are all included by default. The app guards their imports with try/except so a hand-trimmed install still starts, but a standard install has them all. The only genuinely optional add-on is **`crawl4ai`** (JavaScript rendering for crawling SPAs), installed separately via `pip install '.[crawl]'`.

| Package | Install | Adds |
|---|---|---|
| `PyMuPDF` | `pip install PyMuPDF` | PDF text extraction |
| `trafilatura` | `pip install trafilatura` | High-quality HTML-to-text (best web crawl results) |
| `beautifulsoup4` | `pip install beautifulsoup4` | HTML parsing fallback |
| `anthropic` | `pip install anthropic` | Claude native SDK |
| `openai` | `pip install openai` | OpenAI / Qwen / Groq native SDK |
| `google-genai` | `pip install google-genai` | Gemini native SDK |

Without `trafilatura` and `bs4`, only plain-text and Markdown URLs can be crawled. Without `PyMuPDF`, PDF files are rejected at upload. Without provider SDKs, the corresponding provider is disabled with a clear error message.

The browser rendering libraries — marked (Markdown), DOMPurify (SVG sanitising), KaTeX (math), math.js (function graphs), and abcjs (music) — are loaded from CDN in `index.html`; no installation needed.

---

## Acknowledgments & License

RediRecall is built on open-source projects, each under its own license:

- **[Redis](https://redis.io)** *(AGPLv3)* — datastore + vector/query engine
- **Rendering** (all CDN-loaded on first use, never bundled) — [marked](https://marked.js.org) *(MIT)*, [DOMPurify](https://github.com/cure53/DOMPurify) *(Apache-2.0 / MPL-2.0)*, [KaTeX](https://katex.org) *(MIT)*, [math.js](https://mathjs.org) *(Apache-2.0)*, [Chart.js](https://www.chartjs.org) *(MIT)*, [Mermaid](https://mermaid.js.org) *(MIT)*, [Viz.js](https://github.com/mdaines/viz.js) *(MIT; embeds [Graphviz](https://graphviz.org) 15.1.1, EPL-2.0)*, [JSXGraph](https://jsxgraph.org) *(MIT or LGPL-3.0-or-later)*, [Leaflet](https://leafletjs.com) *(BSD-2-Clause)*, [Plotly.js](https://plotly.com/javascript/) *(MIT)*, [SmilesDrawer](https://github.com/reymond-group/smilesDrawer) *(MIT)*, [abcjs](https://www.abcjs.net) *(MIT)*, [highlight.js](https://highlightjs.org) *(BSD-3-Clause)*
- **Map data** — © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, [ODbL](https://opendatacommons.org/licenses/odbl/) (attribution rendered on every map)
- **Backend** — [FastAPI](https://fastapi.tiangolo.com)/[Uvicorn](https://www.uvicorn.org) *(MIT/BSD)*, [redis-py](https://github.com/redis/redis-py) & [RedisVL](https://github.com/redis/redis-vl-python) *(MIT)*, [NumPy](https://numpy.org) *(BSD)*, [sentence-transformers](https://www.sbert.net) *(Apache-2.0)*, [PyMuPDF](https://pymupdf.readthedocs.io) *(AGPLv3)*, [Trafilatura](https://trafilatura.readthedocs.io) *(Apache-2.0)*, [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) *(MIT)*, [Pillow](https://python-pillow.org) *(HPND)*, [httpx](https://www.python-httpx.org) *(BSD)*, [Crawl4AI](https://github.com/unclecode/crawl4ai) + [Playwright](https://playwright.dev) *(Apache-2.0)*, [python-docx](https://python-docx.readthedocs.io) & [openpyxl](https://openpyxl.readthedocs.io) *(MIT)*
- **LLM SDKs** — [Ollama](https://ollama.com), plus the [Anthropic](https://github.com/anthropics/anthropic-sdk-python) *(MIT)*, [OpenAI](https://github.com/openai/openai-python), [Groq](https://github.com/groq/groq-python), and [Google GenAI](https://github.com/googleapis/python-genai) *(Apache-2.0)* SDKs

RediRecall itself is licensed under **AGPL-3.0-or-later**. PyMuPDF and Redis 8 are AGPLv3; all other dependencies are permissive (MIT/BSD/HPND) or Apache-2.0, which are one-way compatible into AGPLv3 — so the combined work is cleanly licensable under the AGPL.
