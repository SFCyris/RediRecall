<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Changelog

All notable changes to RediRecall are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.5] — 2026-08-21

### Added
- **One model picker in the top bar.** Provider and model are chosen together from a
  single control reading `● Ollama / gemma4:31b-mlx`. It lists every model grouped by
  provider with a status dot on each group — green when the provider is reachable, red
  when it is configured but failing — and filters as you type. Providers with no API key
  are collapsed into one row that links to *Settings → Providers*. A provider offering
  more than eight models shows the model in use, `-latest` aliases and stable names
  first, with pinned dated snapshots behind a *Show all N* row. Replaces the seven
  provider buttons and the separate model dropdown.
- **Provider, model and knowledge-base scope are remembered per browser** and restored
  on reload.
- **The top bar is a single row.** It was two, and the second wrapped again as the
  session title and the token count grew — 99px on a fresh conversation, up to 198px in
  use, all of it taken from the chat. It is now a constant 52px at every width, and the
  controls give way in order of value as the window narrows rather than wrapping.
- **Clear chat moved to the sidebar**, beside the other actions that act on the current
  conversation.
- **The provider's default model is marked in the picker** — the free-tier model on
  Gemini and Mistral — in its own colour, with a `default` badge, and sorted directly
  below the model in use so it is never hidden behind *Show all N*.

### Removed
- **The estimated cost figure, everywhere it appeared** — the top-bar cost pill, the
  *Cost* column and partial-total warning in Analytics → Token Usage, the *Cost USD*
  column in the Analytics CSV, and the `pricing` block in `config.json`. The rates
  behind it were shipped constants that no provider supplied and nothing revalidated;
  prices change without notice, so the amount shown could go stale silently while still
  reading as an authority. Token counts are unaffected and remain the provider's own
  reported figures — price them against your provider's billing page.

  An existing `pricing` block in your `config.json` is now ignored. It is left in place
  rather than deleted, so nothing is lost if you kept notes there.

### Fixed
- **Image attachment did not work on any hosted provider.** The 📎 button and the 👁
  Vision badge were decided by looking the selected model up in a list only Ollama ever
  filled, and keyed on a display name while being called with a model id — so attaching
  an image was impossible on Claude, OpenAI, Gemini, Mistral, Groq and Qwen whatever the
  model supported, and pasting one reported *"Switch to a vision model to use images"*
  on a vision model. Hosted model lists now carry a vision flag, taken from the
  provider's own capability report where there is one. Present since 1.0.0.
- **Every page load reset the knowledge-base scope to "All RAGs".** The scope was fixed
  to all-instances at start-up and nothing restored the choice, so a reload silently
  changed what grounded your answers and overrode the instance the configuration
  stored. Present since 1.4.0.
- **Models that cannot hold a conversation were offered as chat models.** Gemini's list
  was filtered on the model id alone, so 37 entries reached the picker of which 21 could
  not answer a question — embedding, text-to-speech, image, robotics, computer-use and
  live-audio models among them. Mistral's list was not filtered at all despite claiming
  to be, adding embedding, OCR, moderation and audio models. Both now use the capability
  each API reports.
- **Switching provider announced success before it knew.** *"Provider: Claude API"*
  appeared the moment the button was pressed, while the model list was still loading and
  might come back empty; nothing said so afterwards, and the top bar showed the new
  provider beside the previous provider's model. The confirmation now names the model
  that was selected, or says the provider has none.
- **Send looked ready with no model selected.** The button stayed lit and the problem
  only surfaced after pressing it.
- **"Refresh Models" in a provider card replaced the active provider's model list**
  with that provider's, leaving the top bar offering models the running provider cannot
  use.
- **Opening *Settings → Ollama* while a hosted provider was active** replaced the active
  model list with Ollama's, silently disabling image attachment until the provider was
  switched.
- **The model list rendered underneath answer cards and tables.** The top bar applies a
  backdrop blur, which confines anything inside it to its own stacking layer, so the open
  menu was painted over by the conversation behind it.
- **A function plot failed whenever a definition's argument was not literally `x`.**
  A log-log relation is written `log(N) = -1.585 * log(x)`, and only `name(x) = …` was
  recognised as a definition — so the whole line was taken as the expression, read as
  *defining* a function called `log`, and every sample came back as a function rather
  than a number. The card reported `Plot error: no finite values`. Any argument name now
  works, and the legend keeps what you wrote. An expression that evaluates to something
  other than a number — writing `sin` where `sin(x)` was meant — now says so and names
  the type, instead of the same generic message.
- **Grouped citation markers were not clickable.** A model citing two passages writes
  `[3, 4]` as readily as `[3] [4]`, and only the lone form was linked — so the grouped
  one read as a citation but opened nothing.
- **The Mistral model list showed bare ids.** Fetched live it dropped the readable
  labels — including the *(free tier)* marks — that the built-in list carries.

## [1.8.1] — 2026-08-21

### Fixed
- **The Documents list failed on Redis Stack.** Listing an instance's documents asked
  the search engine for up to a million rows at once, which a stock Redis Stack refuses
  outright rather than truncating (`MAXAGGREGATERESULTS`, 10000 by default) — so
  *Settings → RAG → Documents* returned an error on that deployment, and the source
  list silently fell back to scanning the whole keyspace on every call. Both now read
  through a cursor and return every document however many there are. Present since
  1.5.1.

## [1.8.0] — 2026-08-21

### Added
- **Token usage in Analytics.** A new *Token Usage* card shows all-time token
  consumption across every conversation, broken down by provider and model, with
  fresh input, prompt-cache reads, cache writes and output as separate columns and
  an estimated cost per model. Rows are ordered by cost, and when a model has no
  entry in the `pricing` table the card names it and says the total is partial. A
  *Reset tally* action clears the counters; conversations and their per-turn counts
  are untouched. Token and cost figures are now included in the Analytics CSV export.
- **Keep an answer.** **💾** on any answer indexes it into a knowledge base, so it outlives
  the semantic cache (one hour by default) and the conversation (one day) — and stays
  *retrievable*, not just readable. A review dialog offers the question, the answer and the
  sources it cited for editing first. Saved answers go to their own `saved-answers`
  instance, created on first use, so they can be switched off when an answer has to be
  grounded only in real documents; each one is a document named `answer://<date> <title>`
  that can be deleted on its own.
- **Scheduled re-crawl, in the interface.** *Settings → Web Sources → Scheduled Re-crawl*
  lists every URL on the timer with its instance, depth and last run, and can schedule the
  current URL, remove one, or re-crawl everything now. The on/off switch and the interval
  are saved with the rest of your settings.
- **File ingestion can be watched and stopped.** An ingest now has a Cancel button, and
  reopening the RAG tab re-attaches to one already running instead of showing an idle
  panel. Cancelling stops before the next file; everything already indexed is kept. On a
  first run the panel says the embedding model is being downloaded rather than sitting at
  an unmoving bar for several minutes.
- **Crawl progress shows real numbers.** With a page limit set the bar fills towards it;
  without one — the default — it shows pages indexed against pages discovered so far, with
  the queue depth beside it, and says which total it is counting against.
- **Citations are clickable.** The `[2]` markers in an answer now open the matching source
  in the sources panel and highlight it.
- **Messages are reviewable.** Errors stay on screen until dismissed instead of vanishing
  after three seconds, every message can be dismissed by hand, hovering pauses the
  countdown, and *Settings → Logs* keeps the session's messages so an error can be read
  after the fact.
- **Search covers more.** It searches retrieved source text as well as messages, can search
  every conversation rather than only the current one, reports how many matches it found
  and highlights each one in context.
- **A first run leads somewhere.** With no model available the welcome screen offers a route
  into provider settings instead of suggestion prompts that cannot work, and the three
  "select a model first" refusals now offer the same route.
- **Unsaved settings are no longer lost silently.** The panel marks itself as having unsaved
  changes and asks before discarding them on Escape, Cancel or a click outside. Sections
  that apply immediately — RAG instances and Redis endpoints — say so.

### Fixed
- Diagram labels containing brackets, parentheses, quotes or `<` no longer blank the
  diagram. Every flowchart node shape is now handled — rectangle, round, stadium,
  subroutine, cylinder, circle, double circle, asymmetric, rhombus, hexagon,
  parallelogram and trapezoid — as well as edge labels written as `-->|text|`, each
  keeping its own shape. A `<` renders as a real `<` in edge labels and subgraph titles.
- A valid diagram using a quoted compound shape, such as `A[("Redis (db)")]` or
  `A[["Sub (x)"]]`, was corrupted into an unrenderable one.
- A node label containing `|`, such as `A[Ratio|Score]`, was corrupted, and could also
  swallow the label of the edge that followed it.
- Diagrams are no longer inserted when the HTML sanitiser is unavailable, matching every
  other rendering lane.
- Cache-creation tokens were missing from the all-time tally, under-reporting cumulative
  cost for prompt-cached conversations.
- A provider error unrelated to token reporting — a mistyped model name, an over-long
  conversation — switched token counting off for that provider until restart.
- API keys are no longer sent in a URL when testing a provider connection, keeping them
  out of server and proxy access logs and out of browser history.
- A Redis endpoint whose name contained quotes or semicolons could run arbitrary script in
  the page when the endpoint list was displayed.
- Deleting or resetting a RAG instance whose name contained `*`, `?` or `[` could affect
  other instances on the same Redis endpoint. Instance names are now validated wherever an
  instance can be created, and the pattern is escaped everywhere it is used.
- Images could be served from a directory whose name merely started with an allowed one.
- Importing a RAG instance stored its chunks outside the search index, so an imported
  instance held all its data and returned nothing from any search. An instance exported
  under a different embedding model now has the affected chunks re-embedded rather than
  stored as vectors the index cannot use.
- Exporting a RAG instance omitted the embeddings of the final partial batch.
- *Export & Delete* waited a fixed moment rather than for the export, so a large instance
  could be deleted before its backup finished. It now waits, keeps the instance if the
  export fails, and asks you to confirm the file actually downloaded — a cancelled save
  cannot be detected by the browser.
- Regenerating an answer could delete the whole conversation when the request carried a
  malformed position.
- Cancelling a crawl left a background worker running for the lifetime of the process and
  discarded the record of pages already indexed.
- Pausing or cancelling a crawl whose address contained a `#` fragment reported success
  while the crawl continued at full speed.
- A tool result containing an image also dumped the raw tool call, including the entire
  image data, below the picture.
- Importing a configuration now asks for confirmation first and reports the actual
  outcome; a failed import previously reported success while changing nothing.
- Exporting the configuration no longer navigates away from the app.
- "Reset Defaults" no longer discards saved web sources, watched folders and endpoints
  without saying so.
- Clearing a conversation, removing a web source, a prompt template or a Redis endpoint,
  and resetting the retrieval counters now ask for confirmation first and say exactly what
  is lost.
- Switching to Claude without a key no longer leaves the model list showing another
  provider's models.
- An ingestion that partly failed is now reported as a warning rather than as neutral
  information.
- Saved web-source URLs and ingestion-log instance names are escaped when displayed.
- Dismissing a "Remove document?" dialog no longer raises a console error.
- Pause and Cancel during a crawl acted on whatever was typed in the URL box rather than
  on the running crawl, so editing that field — which is also where the next crawl is
  composed — left the crawl running while the buttons reported it stopped.
- Re-opening the panel during a crawl showed a fixed 50% that meant nothing, overwrote a
  URL you were part-way through typing, showed a paused crawl as running, and could only
  ever attach to the first of several crawls. It now shows measured progress, leaves the
  URL box alone, names the crawl it is following and offers the others.
- Scheduling a re-crawl before the instance list had loaded stored it against an instance
  no crawl would ever write to.
- *System Status* reported four of the seven providers, so Qwen, Mistral, Groq and Gemini
  users found nothing about the provider they were running on. All seven are listed, an
  unconfigured one is offered a route to set it up, and the per-provider dots on the
  Providers tab — which no code had ever painted — now reflect what is reachable.
- A "⟳ Update dots" button that refreshed nothing has been removed.
- `⌘/Ctrl+F` no longer replaces the browser's own find. The in-app search moved to
  `⌘/Ctrl+Shift+F`.
- The sources panel numbered a chunk by its position on screen while the answer cited its
  position in the retrieved list, so on a reopened or cached conversation `[2]` and `#2`
  could refer to different sources.
- A Cancel button in one confirmation dialog rendered unstyled.
- A diagram node label written with spaces inside its shape, such as `A[ /In (raw)/ ]`,
  failed to render at all when it contained brackets or parentheses.
- A diagram node label with parentheses nested more than one level deep, such as
  `A(a (b (c)) d)`, failed to render.
- A diagram edge label written inline, such as `A -- say "hi" --> B`, failed to render when
  it contained a quote, and showed the literal text `&lt;` where a `<` was meant.
- A timeline containing a time of day failed to render entirely. Writing a timed event as
  `2024-01-01 00:00 : Sunrise` is the natural thing to do, and the colon inside `00:00` was
  read as the period separator; the error it produced pointed at the diagram's first line
  rather than at the colon. Times now render as written, in periods, section labels and
  accessibility lines alike.
- **Accessibility.** Text colours in the light theme now meet the WCAG AA contrast
  minimum, and so does the accent behind white text in both themes; the provider "not
  configured" dot, the selected-model hint, the table filter and the provider name used
  colours that were never defined and fell back to inherited text. Form controls have a
  visible focus outline — which now appears the instant focus lands rather than fading in
  over a quarter of a second — and a distinguishable border. Every field is linked to its
  label, and toggles and icon-only buttons have accessible names. Dialogs declare
  themselves as dialogs, move focus in on open and hand it back on close, keep Tab inside
  themselves,
  and close one layer at a time with Escape; the closed pinned panel is no longer
  reachable by keyboard. Status and error messages are announced, the document has a
  heading and landmarks, message actions are reachable by keyboard and visible on touch,
  and animations — including the streaming avatar — honour the system "reduce motion"
  setting.

## [1.7.0] — 2026-08-12

### Added
- **Real token usage & cost.** Every provider's reported token counts are captured
  per turn, stored with the conversation, and tallied all-time (`GET /api/usage`).
  The top-bar pills show exact input/output/total when the provider reports them
  (the `~` estimate remains only for turns without counts), plus an estimated cost
  from the editable `pricing` table in config.json (approximate defaults for the
  common paid models; free-tier and local models show no cost).
- **Watched folders.** Point a RAG instance at local folders (Settings → Web
  Sources): new and changed supported files are ingested automatically on a
  configurable interval; an edited file replaces its previous version. Files
  deleted from disk stay indexed until removed via Documents.
- **Fork a conversation** (⑂ on any answer): a new session containing everything
  up to that point; the original is untouched.
- **Ollama model management** in Settings → Providers → Ollama: pull a model with
  live progress, and remove installed models.
- **Editable card source.** A rich card's Source view is now editable — Apply
  re-renders the card from the edited spec in place (local only; the stored
  conversation is unchanged); **↺ Reset** discards the edits and restores the
  original.
- **⟲ Reset zoom** on a Mandelbrot/Julia card after a click-to-zoom, returning
  to the spec's own view (mirrors the existing chart zoom-reset).
- **Pan & zoom in Maximize** for diagram cards (mermaid, dot, gantt, timeline,
  SVG): scroll to zoom at the cursor, drag to pan, double-click to reset.
- **Interactive geometry** (✋ on a geometry card): shows the construction's
  control points and lets you drag them; toggle off to return to the clean figure.
- The welcome screen shows the running build — version (linked to its release
  notes), license, and the AGPL source link — reported live by `/api/health`.
- **` ```fractal ` render lane** — Mandelbrot and Julia sets (click to zoom,
  shift-click to zoom out, smooth colouring, four palettes), IFS chaos-game
  attractors and L-system turtle graphics, all drawn on a plain canvas with no
  external library. Presets `fern`, `sierpinski`, `dragon`, `koch`, `plant`,
  `mandelbrot` and `julia` render from a one-line spec; custom affine maps and
  rewrite rules are data-only, with hard caps on iterations, points and
  expansion size.

### Fixed
- Grounded answers judge **relevance** first: retrieved context that does not bear
  on the question is ignored (with a one-line note) and the question is answered
  from general knowledge — a weak vocabulary-overlap match no longer produces an
  answer that summarises an unrelated document and abstains. Each context chunk
  now carries its match score so the model can calibrate.
- `stop.sh` also finds and stops a RediRecall instance started outside the
  scripts (no pidfile) but holding the app port; `start.sh` no longer announces
  the repo's version when a pre-existing instance kept the port — the banner now
  reports what is actually serving.
- `` ```fractal `` no longer fails to render when an IFS probability is written
  as a fraction (`1/3`) — valid maths, invalid JSON. It is now converted to its
  decimal value before parsing.

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

[1.7.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.7.0
[1.6.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.6.0
[1.5.1]: https://github.com/SFCyris/RediRecall/releases/tag/v1.5.1
[1.5.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.5.0
[1.4.1]: https://github.com/SFCyris/RediRecall/releases/tag/v1.4.1
[1.4.0]: https://github.com/SFCyris/RediRecall/releases/tag/v1.4.0
