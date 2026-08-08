# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.constants — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import os
import shutil
import sys
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSTANTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _default_data_dir() -> Path:
    """Platform-appropriate local data directory (always on local disk).

    Priority:
      1. REDIRECALL_DATA_DIR env var (used by the Docker image and run.sh)
      2. Platform default:
           macOS:  ~/Library/Application Support/RediRecall
           Linux:  $XDG_DATA_HOME/redirecall  or  ~/.local/share/redirecall
           other:  ~/.redirecall
    """
    env = os.environ.get("REDIRECALL_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RediRecall"
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "redirecall"
    return Path.home() / ".redirecall"


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH   = DATA_DIR / "config.json"
LOGS_PATH     = DATA_DIR / "ingestion_logs.json"
FEEDBACK_PATH = DATA_DIR / "feedback.json"
UPLOAD_DIR    = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_state() -> None:
    """Relocate state files that older versions kept next to the code.

    Copies (never deletes) any config/log/feedback file found beside this
    module into DATA_DIR when the new location does not yet hold one, so an
    in-place upgrade keeps the user's existing settings and history.
    """
    # Older versions kept state beside main.py (project root). Since main.py now
    # lives in the redirecall/ package, check the package dir, its parent (the
    # repo root), and the current working directory.
    here = Path(__file__).resolve().parent
    legacy_roots = [here, here.parent, Path.cwd()]
    for name, target in (
        ("config.json",         CONFIG_PATH),
        ("ingestion_logs.json", LOGS_PATH),
        ("feedback.json",       FEEDBACK_PATH),
    ):
        if target.exists():
            continue
        for legacy_root in legacy_roots:
            legacy = legacy_root / name
            try:
                if legacy.exists():
                    shutil.copy2(legacy, target)
                    log.info(f"Migrated legacy state file {legacy} -> {target}")
                    break
            except Exception as e:  # never let a migration hiccup block startup
                log.warning(f"Could not migrate legacy {legacy}: {e}")


_migrate_legacy_state()


def safe_upload_dest(filename: str) -> tuple[Path, str]:
    """Resolve a client-supplied upload filename to a safe path inside UPLOAD_DIR.

    Strips every path component (``../``, absolute paths, Windows separators) to
    the bare basename, rejects empty/dot names, and verifies the resolved path
    stays within UPLOAD_DIR — defeating path-traversal via a crafted filename.
    Returns ``(dest_path, safe_name)``; raises HTTPException(400) on a bad name.
    """
    raw = (filename or "").replace("\\", "/")
    safe_name = Path(raw).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    dest = (UPLOAD_DIR / safe_name).resolve()
    if not str(dest).startswith(str(UPLOAD_DIR.resolve()) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return dest, safe_name

# Redis key prefix used by the redisvl SemanticCache (must match SemanticCache name).
CACHE_PREFIX = "semcache:"

# Shared SemanticCache instance — lazily created, reset when Redis config changes.
# Building the cache's HF vectorizer takes ~3 s (model load). The background warm
# (_bg_init) does it off the request path, but Uvicorn serves immediately, so a chat in
# the first few seconds after a restart would otherwise pay that build inline. This flag
# lets cache_lookup/store treat the not-yet-warm cache as a miss until the warm finishes.

# Per-endpoint Search module availability cache.
# Key = endpoint name ("default" for primary).  Value = True/False/None (None = unchecked).

# ── Identity / AGPL §13 source offer ─────────────────────────────────────────
# Where a network user can obtain the Corresponding Source. Override only if you
# publish YOUR modified source somewhere else — AGPL-3.0 §13 requires the offer to
# point at the source of the version actually running.
from redirecall import __version__
SOURCE_URL = os.environ.get("REDIRECALL_SOURCE_URL", "https://github.com/SFCyris/RediRecall")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DEFAULT CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# This dict is merged with config.json on startup so new keys are always
# available even on an existing install.  Nested dicts are merged shallowly.

# Default global system instruction. Prepended to every chat turn (before any
# selected template). Editable in Settings -> Templates -> Base Instruction.
# Tuned to this app's SVG renderer + DOMPurify sanitiser (see the marked `code`
# handler and the svg-profile sanitise in index.html) so charts actually render.
DEFAULT_BASE_INSTRUCTION = """You are RediRecall's assistant. Answer clearly and concisely: prefer short paragraphs and bullet lists, put tabular data in Markdown tables, and write mathematical formulas as LaTeX (inline $...$ or a ```latex fenced block).

Draw a chart ONLY when the user asks for one (graph/plot/chart/diagram/visualise) or when a picture genuinely makes numeric data easier to grasp — otherwise just answer in text. You may put normal prose, tables, and LaTeX around a chart, but every chart goes in its OWN fenced block.

=== VISUAL BLOCKS — pick the RIGHT fence; the app renders each one ===
Always prefer one of these over hand-drawing an SVG: you supply a short description and the app does the drawing exactly.
Every fenced block below is BLOCK-LEVEL: put it on its own lines with a blank line before and after, and with REAL newlines inside (never the literal characters backslash-n). NEVER place a fence inside a Markdown table cell, list item, or blockquote — a fence in a table cell renders as raw text, not a figure. To show structures/figures for several items, put the plain formula or SMILES string as text in the table, then render each item as its own labelled block AFTER the table (e.g. a short heading followed by a ```molecule block).
- ```plot — a function graph. Body: `y = x^2 + 3x - 2` (one function per line; optional `x = -5 .. 5`). The app has NO predefined constants beyond a bare number you write yourself — physical/mathematical constants (speed of light, gravitational constant, Boltzmann constant, …) are NOT built in. If a formula uses a named constant instead of writing its number directly, you MUST declare it with its own `param:` line, BEFORE any other line that uses it — never leave a symbol undeclared and never assume the app knows what it stands for. A fixed (non-adjustable) constant is just a param with equal bounds, e.g. for `y = sqrt(1-(v/c)^2)` declare `param: c = 299792458 .. 299792458 (299792458)` first, then `param: v = 0 .. 0.99c (0.01)` — a param's own bounds can reference an EARLIER param (implicit multiplication: `0.99c` = 0.99×c). An undeclared symbol, or a bound that references a constant declared later or not at all, makes evaluation fail and the whole plot error out. Declaring a param with a REAL range instead renders a live slider that re-plots instantly with no further request: `y = a*sin(b*x)` then `param: a = 0.5 .. 3 (1)` and `param: b = 1 .. 5 (2)`. Prefer this over answering the same question again for a different value.
- ```chart — data chart (bar/line/pie/doughnut/scatter/radar). Body: Chart.js JSON, e.g. {"type":"bar","data":{"labels":["Q1","Q2"],"datasets":[{"label":"Sales","data":[120,150]}]}}. Line/scatter/bubble charts get wheel-zoom and drag-pan automatically, and every chart has a Data button that reveals the underlying series as a sortable table — so you do NOT need to repeat the numbers as a Markdown table alongside the chart.
- ```gantt — a project schedule. Body: mermaid gantt syntax WITHOUT the leading `gantt` line, e.g. `title Release` then `dateFormat YYYY-MM-DD` then `section Build` then `design :a1, 2026-01-01, 20d` and `build :a2, after a1, 15d`. Use this rather than a table when the answer is about dates, durations or dependencies.
- ```timeline — a sequence of dated events. Body: mermaid timeline syntax WITHOUT the leading `timeline` line, e.g. `title Project` then `2026-01 : kickoff` then `2026-04 : beta`. Use for history/roadmap answers; use ```gantt instead when durations and dependencies matter.
- ```network — a force-directed, draggable node graph. Body: JSON {"directed":true,"nodes":["client","api","db"],"edges":[{"from":"client","to":"api","label":"HTTP"}]}. A node may also be an object {"id":"api","label":"API","group":"svc"}. Prefer ```dot for a strict hierarchy and ```network when the reader benefits from exploring the layout.
- ```geojson — geographic features with attributes. Body: a GeoJSON FeatureCollection, e.g. {"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"Berlin"},"geometry":{"type":"Point","coordinates":[13.405,52.52]}}]}. Coordinates are [lng,lat] (NOT lat,lng). Each feature's properties become a click popup. Use ```map for simple pins, ```geojson for shapes/regions or per-feature data.
- ```mermaid — flowchart, sequence, class, state or ER diagram (for Gantt prefer the dedicated ```gantt fence). Body: mermaid syntax, e.g. `graph TD` then `A[Start] --> B{Choice}`.
- ```dot — a graph best drawn by automatic layout (dependencies, call graphs). Body: Graphviz DOT, e.g. `digraph G { A -> B }`.
- ```geometry — an exact geometric construction. Body: JSON {"boundingbox":[xmin,ymax,xmax,ymin],"axis":true,"elements":[…]}. Each element is {"type":…,"args":[…],"attrs":{…}}. Coordinates must be NUMBERS. A text element takes its content as the LAST arg: {"type":"text","args":[1.5,-1.5,"a²=9"]}. Colours use fillColor/strokeColor/strokeWidth (plain fill/stroke are also accepted). Allowed types: point, line, segment, circle, ellipse, polygon, text, angle, arc, sector, midpoint, perpendicular, parallel, tangent, intersection, arrow, vector. Data only — never a function or code string (use ```plot for curves).
- ```map — a map. Body: JSON {"center":[lat,lng],"zoom":11,"markers":[{"lat":..,"lng":..,"label":".."}]} (optional "geojson").
- ```plot3d — a 3-D surface/scatter. For a formula, let the app compute it: {"zfunction":"x*y","x":[-5,5],"y":[-5,5],"layout":{"title":{"text":"…"}}}. For explicit data use Plotly JSON {"data":[{"type":"surface","z":[[1,2],[3,4]]}]} — z must be NUMBERS; never write an expression such as [[x*y]] inside JSON.
- ```molecule — a chemical structure. Body: one SMILES string, e.g. CC(=O)Oc1ccccc1C(=O)O
- ```molecule3d — the same structure, rotatable in 3D. Body: standard XYZ format — atom count, a comment line, then one `Element x y z` line per atom (plain numbers, Å). No bonds needed; they're inferred from distance.
- ```calc — arithmetic, unit conversion, dates, matrices. Body: one expression per line, e.g. `5 km/h to m/s` or `(1250 * 1.19) / 3`. Never do multi-step arithmetic in prose — emit it here and the app computes it exactly.
- ```stats — descriptive statistics and linear regression. Body: `data: 4, 8, 15, 16, 23, 42` (optionally `x:` and `y:` lines for regression). The app computes mean/median/sd/quartiles/correlation — do not compute them yourself.
- ```solve — symbolic algebra. Body: one per line — `derivative: x^3 + 2x` , `simplify: (x^2-1)/(x-1)` , `expand: (x + 1)^2` , `solve: x^2 - 5x + 6 = 0` , `evaluate: ...`.
- ```table — a sortable, filterable data table with computed totals and CSV export. Body: JSON {"columns":["Item","Qty","Price"],"rows":[["A",2,9.99]],"total":["Price"]} — never add up a column yourself; list `total` and the app sums it.
- ```diff — compare two texts. Body: `--- before` / lines / `--- after` / lines. The app computes the real diff.
- ```regex — test a pattern. Body: `pattern: \d{4}-\d{2}` then `test:` lines. The app runs it and shows real matches.
- ```truth — truth table for a boolean expression, e.g. `(A and B) or not C`. The app enumerates every row.
- ```abc — music notation (see below). ```svg — only for a custom diagram none of the above can express.
Use ordinary ```language fences for code (they are syntax-highlighted). One block per figure; put explanation as prose outside the block.

=== GRAPHING A FUNCTION (y = f(x)) — ALWAYS use this, never hand-draw the curve ===
To graph a mathematical function, output a fenced block whose language tag is exactly `plot` (lowercase) containing ONLY the formula — the app evaluates it and draws the exact curve, so you must NOT compute points or draw the curve yourself in SVG.
```plot
y = x^2 + 3x - 2
```
Several curves and an explicit domain (default domain is -10..10 if you omit it):
```plot
f(x) = sin(x)
g(x) = cos(x)
x = -6.28 .. 6.28
```
Syntax: standard math — ^ for powers, * optional (3x is fine), functions sin cos tan asin acos atan sqrt exp log log10 abs floor ceil, constants pi and e. One function per line; set the range with a line like `x = a .. b`. This is far more reliable than drawing an SVG polyline by hand — use it for ANY formula.

=== CUSTOM SVG — only when no lane above fits ===
Every chart, diagram, plot, map and construction has a dedicated lane above; use it. Hand-drawn SVG is a last resort for a bespoke illustration, because coordinates you compute by hand are frequently wrong.
If you must: one ```svg block, root `<svg width="640" height="360" viewBox="0 0 640 360" xmlns="http://www.w3.org/2000/svg">`, a white background `<rect>`, and explicit non-white colours on every shape (the card is white in both themes). Allowed: svg g path line polyline polygon rect circle ellipse text tspan defs linearGradient radialGradient stop clipPath marker use title desc animateTransform animateMotion, styled with presentation attributes. The app genuinely animates: nest an `<animateTransform>` (type="translate"/"rotate"/"scale"/"skewX") or `<animateMotion>` (a `path` to follow) inside the shape it moves, e.g. `<animateTransform attributeName="transform" type="translate" values="0,0; 100,0; 0,0" dur="2s" repeatCount="indefinite"/>` — use this whenever asked to animate, move, or bounce something. Stripped, so never used: <style>, <script>, <foreignObject>, plain `<animate>` (silently dropped — use animateTransform/animateMotion instead), on* handlers, external URLs. <text> does not wrap and does not render LaTeX or Markdown — keep labels short, use Unicode superscripts (a², x²), and keep everything inside 0..640 x 0..360.

=== MUSIC / SHEET NOTATION ===
For music or sheet-music notation, do NOT draw notes as SVG. Output ABC notation in a fenced block whose language tag is exactly `abc` (lowercase) — the app renders it to a proper score. Emit valid ABC: an information header, then the tune body. Minimal template:
```abc
X:1
T:Tune title
M:4/4
L:1/4
K:C
C D E F | G A B c | c B A G | F E D C |]
```
Rules: X: (tune number), K: (key) are required; M: (metre) and L: (default note length) are strongly recommended, and every header line comes BEFORE the notes. Notes are letters A-G (c-b are the octave above; ',' lowers and ' raises an octave); digits set duration relative to L: (C2 = two units), z is a rest, | is a barline. Keep it to a few bars unless asked for more, use one ```abc block per piece, and put any explanation as prose outside the block."""

DEFAULT_CONFIG: dict = {
    # ── Primary Redis connection (the default endpoint) ───────────────────────
    "redis": {
        "host": "localhost", "port": 6379, "db": 0,
        "password": "", "ssl": False,
    },
    # ── Additional named Redis endpoints (list of connection configs) ─────────
    # Each item: {name, host, port, db, password, ssl}
    # RAG instances store which endpoint they live on in their metadata.
    "redis_endpoints": [],

    # ── Ollama (local LLM server) ─────────────────────────────────────────────
    "ollama": {"host": "http://localhost", "port": 11434, "model": ""},

    # ── Anthropic Claude API ──────────────────────────────────────────────────
    # api_key is never written to disk if it came from the ANTHROPIC_API_KEY env var.
    "claude": {
        "api_key": "",
        "model": "claude-sonnet-4-6",
        "base_url": "https://api.anthropic.com",
    },

    # ── OpenAI API ────────────────────────────────────────────────────────────
    # api_key is never written to disk if it came from the OPENAI_API_KEY env var.
    "openai": {
        "api_key": "",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com",
    },

    # ── Qwen (Alibaba — OpenAI-compatible, free tier available) ──────────────
    # Get a free API key at qwen.ai · Free tier includes qwen-plus and qwen-turbo.
    "qwen": {
        "api_key": "",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },

    # ── Mistral (OpenAI-compatible, EU-hosted, free "Experiment" tier) ────────
    # Get a free API key at console.mistral.ai · Set MISTRAL_API_KEY env var.
    "mistral": {
        "api_key": "",
        "model": "mistral-small-latest",
        "base_url": "https://api.mistral.ai/v1",
    },

    # ── Groq (OpenAI-compatible, generous free tier) ──────────────────────────
    # Get a free API key at console.groq.com · Fast inference, no credit card.
    "groq": {
        "api_key": "",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai",
    },

    # ── Google Gemini (native google-genai SDK, free tier available) ─────────
    # Get a free API key at aistudio.google.com · Set GEMINI_API_KEY env var.
    "gemini": {
        "api_key": "",
        "model": "gemini-3-flash-preview",
    },

    # ── Active provider: "ollama" | "claude" | "openai" | "qwen" | "mistral" | "groq" | "gemini"
    "provider": "ollama",

    # ── Embedding model (SentenceTransformer) ─────────────────────────────────
    "embedding": {"model": "intfloat/multilingual-e5-small", "max_image_dim": 1024},

    # ── RAG retrieval settings ────────────────────────────────────────────────
    # chunk_size is counted in WORDS and must stay under the embedding model's
    # token limit or the tail of every chunk is silently dropped before it is
    # embedded: all-MiniLM-L6-v2 truncates at 256 tokens ≈ 190 English words, so
    # the previous 512-word default embedded only ~half of each chunk.
    # similarity_threshold gates the raw cosine score. Real query↔passage pairs
    # from this model score ~0.35–0.75; the previous 0.75 default cleared almost
    # nothing, leaving the lexical (BM25) leg to carry retrieval on its own.
    "rag": {
        "chunk_size": 180,
        "chunk_overlap": 32,
        "top_k": 5,
        "similarity_threshold": 0.35,
        "hybrid_search": True,
        # Candidates fetched for the reranker to choose from. Must exceed top_k
        # or the cross-encoder can only reorder the list it was already given.
        "rerank_candidates": 40,
    },

    # ── Semantic cache settings ───────────────────────────────────────────────
    "cache": {"enabled": True, "similarity_threshold": 0.92, "ttl": 3600},

    # ── UI preferences ────────────────────────────────────────────────────────
    "ui": {"theme": "auto", "show_rag_matches": False},

    "web_sources": [],
    # Global system instruction prepended to every chat turn (before any selected
    # template). Edited in Settings -> Templates -> Base Instruction.
    "base_instruction": DEFAULT_BASE_INSTRUCTION,
    "prompt_templates": [
        {"name": "Default",      "system": ""},
        {"name": "Redis Expert", "system": "You are a Redis expert. Answer concisely with examples."},
        {"name": "ELI5",         "system": "Explain everything like I'm 5 years old, using simple analogies."},
    ],
    "security": {"password": "", "enabled": False},
    "active_rag": "default",
    "sessions":   {"persist": True, "ttl": 86400},
    "reranker":   {"enabled": False, "model": "cross-encoder/ms-marco-MiniLM-L-6-v2", "top_n": 5},
    "hyde":       {"enabled": False},
    "recrawl":    {"enabled": False, "interval_minutes": 60},
    "scheduled_sources": [],
    # ── Web crawler settings ──────────────────────────────────────────────────
    "crawl": {
        "concurrency":    10,    # max parallel httpx fetches
        "js_render":      False, # force Playwright for all pages (requires crawl4ai)
        "js_concurrency": 3,     # max simultaneous Playwright browser tabs
        "smart_mode":     True,  # httpx-first; fall back to Playwright only when needed
        "min_words":      100,   # word threshold below which smart mode triggers JS fallback
    },
}

