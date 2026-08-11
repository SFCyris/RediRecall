<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Changelog

All notable changes to RediRecall are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] — 2026-08-10

### Added
- The RAG selector has a **No RAG** option: the model answers from its own
  knowledge with no retrieval and no grounding instructions, distinct from
  querying one instance or all of them.
- Settings → Templates has an **"Include chart/diagram authoring rules"** toggle.
  Turn it off on text-only deployments to stop sending the ~2,000-token chart/plot/
  diagram/map/music authoring section to the model on every message.
- Settings → RAG has a **Chat History Budget** control that caps how much recent
  conversation is resent to the model each turn, and a note showing how Top-K and
  Chunk Size translate into tokens per grounded answer.
- The top-bar token estimate is split into **input** (your prompts), **output**
  (the model's replies) and **total**, each a colour-coded pill.

### Changed
- Retrieved context and uploaded-file text now ride on the question turn instead of
  the system prompt, so the system prefix stays stable across a conversation. This
  lets the provider cache it (billed input tokens re-read far cheaper on repeat
  turns for Claude and the OpenAI-compatible providers) — no behaviour change to
  answers. Responses (the app UI and JSON API) are now gzip-compressed.

### Fixed
- `` ```geometry `` blocks render many more constructions: rectangles, circles from
  a centre and radius, arcs and sectors from a centre/radius/angle, and dashed lines.
  An element the renderer cannot build is now skipped with a note at the foot of the
  card, instead of the whole figure failing to render.
- `` ```mermaid `` flowcharts no longer fail to render when a node or edge label
  contains parentheses, quotes, a `<`, or other special characters.

## [1.5.1] — 2026-08-08

### Added
- Each question you asked now carries its own action bar (on hover): **Copy** puts
  the query text on the clipboard, **Rerun** asks it again (a matching cached answer
  may be served), and **Force rerun** asks it again while bypassing the cache for a
  fresh answer.
- Function plots (` ```plot `) now list each function's definition —
  `name(x) = expression` — colour-matched to its curve, in a block in front of the
  graph. A legend of bare `f(x)`, `g(x)`, `h(x)` no longer hides what each function
  is. The domain line is not treated as a function.

### Fixed
- The JavaScript test helpers ran snippets via `node -e`, whose single-argument
  size limit on Linux (128 KB) failed the CI `tests` workflow on large snippets
  while passing on macOS; they now run the program from a temp file.

## [1.5.0] — 2026-08-07

Upgrading from an earlier version keeps working immediately; the search index
migrates itself on first start (v4 → v5, no data dropped). To benefit from the new
default embedding model, existing corpora must be re-ingested — until then they
keep working with the model they were built with.

### Added
- Four render lanes: `gantt`, `timeline`, `network` (force-directed, draggable) and
  `geojson` (Leaflet with per-feature popups).
- Every Markdown table an answer produces is now sortable (by value, including
  currency and dates), filterable, and exportable to CSV, with a sticky header.
- Charts support wheel-zoom (hold Ctrl) and drag-to-pan with a Reset button, and a
  Data button that reveals the underlying series as a sortable table.
- `abc` music scores have a Play button.
- Embedding models `intfloat/multilingual-e5-base` and `BAAI/bge-m3` are selectable
  in Settings, alongside the new default.
- Each chunk records which embedding model produced its vector, so an instance can
  hold vectors from more than one model.
- Crawls can be paused and resumed, not only cancelled.
- Backup and restore procedure documented in `DOCS.md`.
- A mutation-tested regression suite (`tests/mutation_sweep.py`,
  `tests/mutations.json`) that fails the build if a catalogued defect can be
  reintroduced without a test going red.

### Changed
- Default embedding model is now `intfloat/multilingual-e5-small` (384-dimensional,
  512-token window). It covers 100+ languages; the previous default tokenised many
  non-Latin scripts as `[UNK]`. New installs only — existing installs keep their
  model until changed.
- Frontend libraries upgraded: `marked` 9 → 16, DOMPurify → 3.4.11, KaTeX → 0.17.0,
  `vis-network` → 10.1.0, and `viz.js` 2.1.2 → `@viz-js/viz` 3.29.0.
- HNSW search breadth (`EF_RUNTIME`) raised to 128, restoring full recall against
  exact search on the reference corpus.
- Container runs as a non-root user; the bundled Redis has a memory bound and an
  eviction policy.

### Fixed
- A `dot`/Graphviz graph using `style="rounded,filled"` rendered as solid black
  boxes with invisible labels.
- A sentence containing both currency and inline math (`Refund $20 if the error
  $e$ …`) typeset the prose as math and left the real math as plain text.
- Line, scatter and bubble charts could zoom to a zero-width range and become blank
  with no way to recover except reloading.
- The chart Data table was empty for scatter and bubble charts.
- Molecule structures were clipped by their card.
- The `geometry` lane's labels were unreadable in dark mode.
- `solve` with `expand:` returned the input unchanged instead of expanding it.
- Cached answers were missing from the saved transcript, breaking session restore,
  regenerate, the version switcher and feedback.
- The "no knowledge-base match" warning appeared on answers where retrieval never
  ran.
- The semantic cache could replay an answer produced with an instance switched off.
- Per-document delete was case-insensitive and did not release crawled URLs.
- `plot3d` titles were dropped under the upgraded Plotly.
- `abc` playback fetches its soundfont from a host the Content-Security-Policy now
  permits; the README's CSP description was corrected to match.
- `THIRD-PARTY-LICENSES.md` completed, including the FluidR3_GM soundfont
  (CC-BY-3.0) attribution and the corrected Graphviz (EPL-2.0) entry.

## [1.4.1] — 2026-08-02
- The `redirecall` command-line entry point ignored its arguments; `--help` started
  a server and hung instead of printing help, and `--port`/`--host` were discarded.

## [1.4.0] — 2026-08-01
- Multilingual embedding default introduced, regression suite added, and a batch of
  retrieval, ingestion and UI defects fixed.

## Earlier releases

See the [GitHub releases](https://github.com/SFCyris/RediRecall/releases) for
1.3.x and 1.2.0.

[1.6.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.6.0
[1.5.1]: https://github.com/SFCyris/RediRecall/releases/tag/v1.5.1
[1.5.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.5.0
[1.4.1]: https://github.com/SFCyris/RediRecall/releases/tag/v1.4.1
[1.4.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.4.0
