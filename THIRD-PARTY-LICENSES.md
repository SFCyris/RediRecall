# Third-party licenses

RediRecall is licensed under **AGPL-3.0-or-later**. This file lists the third-party components it depends on and their licenses.

_Generated on 2026-07-26 from the resolved dependency closure of a development install._

> **Scope note.** The table below is the closure as resolved on macOS. The Linux images resolve a slightly different set, because some dependencies are selected by platform marker — see **CPU-only PyTorch** below, which is a licensing-relevant difference, not just a size one.

## Python dependencies (bundled in the Docker image)

| License | Packages |
|---|---|
| **MIT** | `PyYAML`, `annotated-doc`, `annotated-types`, `anthropic`, `anyio`, `beautifulsoup4`, `brotli`, `cffi`, `charset-normalizer`, `docstring_parser`, `et_xmlfile`, `fastapi`, `fastapi-cli`, `filelock`, `h11`, `jiter`, `markdown-it-py`, `mdurl`, `openpyxl`, `pydantic`, `pydantic_core`, `python-docx`, `python-ulid`, `pytz`, `redis`, `redisvl`, `rich`, `rich-toolkit`, `setuptools`, `six`, `soupsieve`, `tqdm`, `typer`, `typing-inspection`, `tzlocal`, `urllib3`, `zipp` |
| **Apache-2.0** | `aiofiles`, `courlan`, `cryptography`, `distro`, `google-auth`, `google-genai`, `groq`, `hf-xet`, `htmldate`, `huggingface_hub`, `importlib_metadata`, `jsonpath-ng`, `ml_dtypes`, `openai`, `orjson`, `packaging`, `python-dateutil`, `python-multipart`, `requests`, `safetensors`, `sentence-transformers`, `sniffio`, `tenacity`, `tokenizers`, `trafilatura`, `transformers`, `uvloop` |
| **BSD** | `Jinja2`, `MarkupSafe`, `Pygments`, `babel`, `click`, `dateparser`, `fsspec`, `httpcore`, `httpx`, `idna`, `joblib`, `jusText`, `lxml`, `mpmath`, `networkx`, `numpy`, `pyasn1`, `pyasn1_modules`, `pycparser`, `scikit-learn`, `scipy`, `starlette`, `sympy`, `threadpoolctl`, `torch`, `uvicorn`, `websockets` |
| **AGPL-3.0** | `PyMuPDF`, `PyMuPDFb` |
| **ISC** | `dnspython`, `shellingham` |
| **Apache-2.0 (with CPython-derived portion)** | `regex` |
| **MIT-CMU** | `pillow` |
| **MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later** | `tld` |
| **MPL-2.0** | `certifi` |
| **PSF-2.0** | `typing_extensions` |
| **Unlicense** | `email-validator` |
| **see package** | `ujson` |

Notes on the non-obvious entries:

- **`PyMuPDF` / `PyMuPDFb` — AGPL-3.0.** Same copyleft family as RediRecall, which is a large part of why this project is AGPL. Using RediRecall under other terms would require replacing PyMuPDF or obtaining a commercial license from Artifex.
- **`tld` — tri-licensed `MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later`** (pulled in by `trafilatura` → `courlan`). RediRecall elects the **LGPL-2.1-or-later** option. LGPL-2.1-or-later can be used under LGPL-3.0/GPL-3.0 terms, which combine with AGPL-3.0; the GPL-2.0-only option is *not* elected, because GPLv2-only would be incompatible with AGPLv3. `tld` is used unmodified as a library.
- **`regex` — Apache-2.0**, except for code derived from CPython's `re` module, which carries CNRI's Python 1.6 license. The additions and the package as distributed are Apache-2.0; the CPython-derived portion is the same code shipped in every CPython distribution.
- **`ujson` — BSD-3-Clause** (ESN / Electronic Arts). Its packaging metadata omits the license field; the license text is in the distribution's `LICENSE.txt`.
- **`certifi`, `orjson`, `tqdm` — MPL-2.0** (in whole or part). MPL-2.0 is explicitly compatible with the GPL/AGPL family.
- **`Pillow` — MIT-CMU**, a permissive MIT/HPND-style license.

### CPU-only PyTorch (licensing-relevant)

`sentence-transformers` requires `torch`, and on Linux the **default PyPI `torch` declares NVIDIA CUDA runtime wheels** (`cuda-bindings`, `cuda-toolkit`, `nvidia-cudnn-*`, `nvidia-nccl-*`, `nvidia-cusparselt-*`, `nvidia-nvshmem-*`). Those are **proprietary** — `LicenseRef-NVIDIA-SOFTWARE-LICENSE` / "NVIDIA Proprietary Software" — so bundling them into a redistributed AGPL-3.0 image would combine proprietary binaries with copyleft code. (As of `torch` 2.13 the CUDA requirements apply to **arm64 as well as x86_64**; earlier versions gated them on `platform_machine == "x86_64"`.)

The [`Dockerfile`](Dockerfile) therefore installs **`torch` from PyTorch's CPU-only index** (`https://download.pytorch.org/whl/cpu`) *before* `pip install .`, so no NVIDIA wheel is ever pulled into the published image. This keeps the distributed artifact free of proprietary components — and incidentally removes roughly 2 GB. Embeddings run on CPU inside the container; build your own image if you want GPU acceleration (in which case the resulting image is yours to license and distribute, or not).

## Browser rendering libraries (not redistributed)

These are **not** bundled with RediRecall or included in the Docker image. `redirecall/index.html` contains only URLs; the end user's browser fetches each library from a public CDN the first time a block of that type is rendered.

| Library | Used for | License |
|---|---|---|
| [marked](https://marked.js.org) | Markdown | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) | sanitising SVG / rendered HTML | Apache-2.0 or MPL-2.0 |
| [KaTeX](https://katex.org) | LaTeX math | MIT |
| [math.js](https://mathjs.org) | `plot` function graphs | Apache-2.0 |
| [Chart.js](https://www.chartjs.org) | `chart` data charts | MIT |
| [chartjs-plugin-zoom](https://github.com/chartjs/chartjs-plugin-zoom) | `chart` pan/zoom | MIT |
| [Hammer.js](https://hammerjs.github.io/) | touch-gesture recognition for the `chart` zoom plugin | MIT |
| [Mermaid](https://mermaid.js.org) | `mermaid` diagrams | MIT |
| [@viz-js/viz](https://github.com/mdaines/viz.js) | `dot` graph layout | MIT — embeds [Graphviz](https://graphviz.org) 15.1.1 (EPL-2.0) and Expat (MIT) |
| [JSXGraph](https://jsxgraph.org) | `geometry` constructions | MIT or LGPL-3.0-or-later (**MIT elected**) |
| [Leaflet](https://leafletjs.com) | `map` maps | BSD-2-Clause |
| [Plotly.js](https://plotly.com/javascript/) | `plot3d` 3-D plots | MIT |
| [SmilesDrawer](https://github.com/reymond-group/smilesDrawer) | `molecule` structures | MIT |
| [3Dmol.js](https://3dmol.csb.pitt.edu/) | `molecule3d` 3D structures | BSD-3-Clause |
| [abcjs](https://www.abcjs.net) | `abc` sheet music | MIT |
| [vis-network](https://github.com/visjs/vis-network) | `network` force-directed graphs | Apache-2.0 or MIT |
| [highlight.js](https://highlightjs.org) | code syntax highlighting | BSD-3-Clause |

**Graphviz / EPL-2.0.** `@viz-js/viz` is MIT-licensed but embeds Graphviz — version **15.1.1** in the `@viz-js/viz@3.29.0` build loaded here (the `graphvizVersion` string compiled into `viz-global.js`). Graphviz relicensed to the **Eclipse Public License 2.0** at **14.1.4** (early 2026; it was the Common Public License 1.0 before that, which downstream tools often labelled EPL-1.0), so every 14.1.4-or-later build — including the 15.1.1 embedded here — is EPL-2.0, a license the FSF still regards as GPL-incompatible. EPL-2.0 adds a "Secondary Licenses" mechanism that can grant GPL compatibility, but Graphviz did not elect it: Exhibit A of its `COPYING` is left as the unfilled `{name license(s)…}` boilerplate, so the GPL-incompatibility conclusion holds. Because RediRecall neither bundles nor conveys it (the browser loads it from a CDN at runtime), it does not form a combined work with this AGPL codebase. Anyone who chooses to **vendor** the browser libraries into a distributed build should review that themselves; the `dot` lane can simply be dropped if that is a concern. Note that EPL-2.0 §3.3 forbids stripping the `Copyright (c) Michael Daines … Graphviz, Expat` header from `viz-global.js` if the file is ever copied into a build.

## Services

- **Redis 8** — tri-licensed RSALv2 / SSPLv1 / **AGPLv3**; RediRecall assumes the AGPLv3 option. Redis runs as a **separate process/service** reached over the network, not linked into this program.
- **OpenStreetMap** — map tiles for the `map` block. Map data © OpenStreetMap contributors, licensed under the [ODbL](https://opendatacommons.org/licenses/odbl/); attribution is displayed on every rendered map. This is one of two render paths that reach a third-party host at runtime (the other is the `abc` soundfont below); the `map` lane is the only one that sends *content-derived* data — the requested tile coordinates.
- **FluidR3_GM soundfont (CC-BY-3.0 — attribution required).** The `abc` Play button synthesises audio with abcjs, which fetches General-MIDI instrument samples at play time from `https://paulrosen.github.io/midi-js-soundfonts/FluidR3_GM/` (the [midi-js-soundfonts](https://github.com/gleitz/midi-js-soundfonts) project's `gh-pages`). The **FluidR3_GM** SoundFont was created by **Frank Wen** and is distributed there under the **Creative Commons Attribution 3.0** license ([CC-BY-3.0](https://creativecommons.org/licenses/by/3.0/)) — this notice is the attribution that license requires. The samples are static files fetched over GET; no content-derived data is sent. If you do not need `abc` audio playback, remove `https://paulrosen.github.io` from `connect-src` in `_CSP` (`redirecall/main.py`) and the fetch never happens.
- **LLM providers** (Ollama, Anthropic, OpenAI, Qwen, Mistral, Groq, Gemini) are contacted over their APIs. Calling a network API creates no license obligation for RediRecall; the client libraries are listed above — `anthropic` (MIT), `openai` (Apache-2.0, which also drives the OpenAI-compatible Qwen, Mistral and Groq endpoints) and `google-genai` (Apache-2.0). Ollama is reached over plain HTTP via `httpx` (BSD), with no vendor SDK.

## How this list was produced

The Python table reflects the **installed dependency closure** — what actually ships — rather than only the packages declared in `pyproject.toml`. It was built by walking the runtime requirements of the declared dependencies and reading each distribution's own metadata (`License-Expression`, license classifiers) and bundled licence files, so entries with missing or misleading metadata (such as `ujson`) were resolved from the licence text itself.

The optional `[crawl]` extra (`crawl4ai` + Playwright, for JavaScript-rendered crawling) is **not** installed by default or included in the Docker image. Its dependency closure was resolved separately under a Linux/x86-64 marker environment — 90 packages, all permissive (MIT / BSD / Apache-2.0 / MPL-2.0), with **no proprietary, GPL-2.0-only, EPL, CDDL or SSPL component**, and notably no CUDA: `torch` reaches the project through `sentence-transformers` in the core, not through this extra.
