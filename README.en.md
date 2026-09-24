# NTE-RAG — a local RAG knowledge assistant for *Neverness to Everness*

[![CI](https://github.com/chaly27yhy/nte-rag/actions/workflows/ci.yml/badge.svg?branch=main&label=CI)](https://github.com/chaly27yhy/nte-rag/actions/workflows/ci.yml)

[中文说明（主文档）](README.md) | **English**

> This build is **Chinese-first**: the bundled data, the built-in sources and the retrieval layer are
> Chinese-only. This file is a summary for readers who do not read Chinese; the Chinese
> [README.md](README.md) is the authoritative document and is far more detailed.
>
> Every other document in the repository is Chinese-only as well: `CONTRIBUTING.md`, `SECURITY.md`,
> `CODE_OF_CONDUCT.md`, `DATA_LICENSE.md`, `THIRD_PARTY_NOTICES.md` and everything under `docs/`.
> There are no English versions of the others, and no plans to keep two languages in sync.

A single-file, no-install **Windows** desktop tool for *Neverness to Everness* (Chinese title 《异环》,
also written **NTE**). Point it at one of the 17 preset providers, or at a model server on your own
machine (Ollama / LM Studio / vLLM) if you want the model call to stay local too. It answers from a
bundled Chinese knowledge base. When the local material is not enough it searches the web, scrapes the
pages and writes the new knowledge back into the local KB, so the next identical question is answered
locally. With no model configured it degrades to a raw-excerpt mode, which is a fallback rather than the
intended use.

- 17 preset providers (China-direct, overseas, local/self-hosted), or your own endpoint. The app ships
  **no model, no key and no proxy**.
- Packaged as one `.exe`: double-click to run. No Python and no dependencies to install.
- **No developer API key is included.** Your key is encrypted with the Windows DPAPI and stays on your
  own machine.
- Portable mode: put your data next to the exe and carry the whole folder on a USB stick.
- Repository name `nte-rag`; `yihuan`, `NTE` and `Neverness to Everness` all refer to the same game.

**What it is not.** It is **not** an English knowledge base. Everything it knows is Chinese: the bundled
seed data (685 structured facts + 152 source excerpts from 70 documents — one of those documents is on the
withdrawal list and is skipped on import, so the app itself shows 682 facts + 150 excerpts from 69
documents), the 15 built-in sources, and
the Chinese bigram + SQLite FTS5 retrieval layer. FTS5 matches *strings, not meanings*, so an English
question retrieves nothing from Chinese entries. The UI could be translated cheaply; the data cannot.
English support would require a bilingual data layer, which this version does not have.

It is also **not** an official product, and it ships no game art. Every icon is drawn in code.

## Requirements

| | |
|---|---|
| OS | Windows 10 / 11 (there is no macOS or Linux path anywhere in the code) |
| Released exe | nothing else — no Python, no Visual C++ redistributable to install by hand |
| Rendering | Microsoft Edge **WebView2 Runtime**, standard on current Windows 10/11. If it is missing the app says so and falls back to your default browser instead of failing silently |
| Working from source | Python 3.12 + PowerShell 5.1 + PyInstaller |

## Quick start — released build

1. Download `NTE-RAG-onedir.zip` from Releases and unzip it (it expands into a `NTE-RAG\` folder).
2. Run `NTE-RAG.exe` **from inside that folder**.
3. On the 设置 (Settings) page pick a provider and paste your API key.
4. Ask something. If the local material is not enough, the app will offer to search the web.

`NTE-RAG.exe` (the single-file build) is published alongside the zip; the release notes carry the
SHA-256 hashes and byte sizes of both. Take the zip if you want the more
compatible option: some security products and locked-down corporate accounts block the single-file
build from unpacking itself into `%TEMP%`.

Current release **v1.0.0**, bundled data snapshot **2026-09** (the app shows it on the About page).
The byte size and SHA-256 of each artifact are listed in the Chinese README section
「发布产物与哈希校验」 (see [`README.md`](README.md)).

## Quick start — from source

```powershell
git clone https://github.com/chaly27yhy/nte-rag.git    # then: cd nte-rag
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe run.py --selftest    # 11 checks, no model and no network needed
.venv\Scripts\python.exe run.py               # start the desktop window
```

Install with **the venv's own interpreter**, as above. Running the system `python -m pip install`
instead would put the dependencies in the global `site-packages` while `run.py` uses the one in
`.venv`, and you would get `ModuleNotFoundError` at startup.

In restricted environments (locked-down corporate accounts, sandboxes) `ensurepip` may be blocked, so
the venv has no `pip` at all, and `tempfile.mkdtemp()` returns a `0o700` directory that pip cannot
unpack wheels into. The fallback is:

```powershell
python -m venv .venv
# use a Python that *does* have pip, and install straight into the venv's site-packages
python tools\pip_runner.py install --target .venv\Lib\site-packages -r requirements-dev.txt
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the `fetch_pure_sdist.py` fallback used for old
`setup.py`-only packages such as pywebview's `proxy_tools` dependency.

## Safety and privacy

The HTTP server binds to `127.0.0.1` only, and a `Host`-header guard rejects non-loopback requests
(defence against DNS rebinding). An `HttpOnly` + `SameSite=Strict` cookie token keeps other processes
on the same machine out.

API keys are encrypted with the Windows DPAPI before they are written to `config.json`. That file is
never copied anywhere else, and there is **no telemetry, no analytics and no upload path**.

Typing a provider URL and key on the Settings page and pressing "Fetch models" or "Test connection" uses
exactly those values — there is no need to save first. The one exception is switching provider, or moving
the Base URL to another host: the app refuses and asks you to paste the key again, instead of sending a
saved key to an address that only the settings form supplied. Changing just the path on the same host
needs no re-typing.

The tool answers from the local knowledge base first. When the local evidence is not enough it searches
the web **automatically** — `answer.auto_web` in Settings defaults to on, and the chat page has a
「资料不足时自动联网」 checkbox for the current question. When it does search, it obeys `robots.txt`
fail-closed, paces requests per host, honours `Retry-After`, and refuses loopback or private-network
targets.

There is no program self-updater anywhere in the code: nothing is downloaded and executed. The built-in
"auto update" feature only fetches public web pages and writes facts into your local knowledge base; it
never replaces or runs code.

## Data sources and licensing

The code is **MIT** (`LICENSE`). The bundled knowledge is not. The summary facts and the source excerpts
stay the property of the sites they came from: the official site, BWIKI, Moegirlpedia, a second-source
wiki, and two community guide sites. `DATA_LICENSE.md` lists exactly what is bundled — counts, lengths,
per-domain breakdown — how it is used, and how to have an entry removed. An Issue template exists for
that; removal comes first, discussion later, no legal paperwork required.

Upstream dependency licences are listed per package in `THIRD_PARTY_NOTICES.md`.
If you fork this project and want to avoid the question entirely, delete `seed/seed_kb.json`. The app
starts fine without it and simply begins with an empty knowledge base.

Content that review finds untrustworthy is withdrawn page by page. `app/core/curation.py` keeps a short
list of such pages — for example a wiki character page whose skill text, story text, birthday and voice
actor were each copied from three other pages of the same wiki. Documents, chunks and facts from a listed
page are marked `revoked`, so retrieval (which only reads `active`/`conflict` rows) cannot use them, while
the records stay in the database for audit and the answer cites the curated value instead. The list is
applied to existing knowledge bases at startup, keyed by a fingerprint of the list itself.

## Re-theming: the framework is generic, the data is not

Swapping the source list (`app/core/sources.py`) and the default topic words (`app/config.py`) is
enough to point the pipeline at another game or subject. Retrieval, answering, trust scoring and
cross-source adjudication do not care what the topic is. Two things do:

- The bundled knowledge base is still about *Neverness to Everness* (`seed/seed_kb.json`,
  70 documents / 685 facts). It does not disappear when you change the topic; it turns into **wrong
  evidence** for the new one. Delete it (or drop in your own seed) and re-ingest before trusting any
  local answer.
- The collectors are game-specific. `app/core/wywyx.py` parses the character-panel fields of one
  Chinese guide site, and `app/core/wiki_api.py` depends on biligame wiki templates and category
  names. Expect them to extract nothing (or garbage) for a different subject until the parsers are
  rewritten.

So the framework is generic, while the bundled data and the collectors are not.

## Development and tests

```powershell
.venv\Scripts\python.exe tools\quality_check.py      # 698 assertions, no model, no network
node --check app\web\app.js                          # front-end syntax gate (needs Node.js)
.venv\Scripts\python.exe run.py --window-test         # can a native window be created?
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1   # builds dist\
```

`tools/quality_check.py` imports only the app package plus the standard library, so it runs as a CI job
on `windows-latest` — which is exactly what `.github/workflows/ci.yml` does.

The evaluation sets (60 curated questions + API and table suites) live in `eval/`; see
[`eval/README.md`](eval/README.md). Scoring requires a configured LLM.

This README stays focused on using the app. The in-depth development material — environment setup, the
development-time `.env`, the bundled diagnostic tools, rebuilding the seed library, packaging and
release steps, platform caveats, the entry-point explanation and every pitfall hit along the way —
lives in [`docs/development.md`](docs/development.md) and is **Chinese-only** (the project is
Chinese-first; see "Known limitations").

## Project layout

```
run.py                  entry point (also the PyInstaller entry)
app/main.py             server + window + self-test orchestration
app/core/               storage, retrieval, crawling, LLM, trust & consistency
app/server/api.py       the local HTTP API (35 routes)
app/web/                the UI — plain HTML/CSS/JS, no build step
tools/                  quality checks, packaging, data auditing, evaluation
seed/seed_kb.json       the bundled knowledge base
```

## About this project

- Bring your own LLM API. The app is a *Neverness to Everness* question-answering front end for a
  model you choose: 17 presets, or your own server, including a local one. The bundled Chinese
  knowledge base is what makes the answers specific to this game. The no-model "raw excerpts" mode is a
  fallback, not the intended use.
- Maintained by one person. Issues and PRs are read, but no response time is promised.
- Windows-only and Chinese-first. There is no macOS/Linux path, and no English data; see "What it is
  not" above.
- The bundled knowledge comes from public sites: game wikis, the official site, games media. Each
  domain is credited. Licensing, attribution and the removal process are in
  [`DATA_LICENSE.md`](DATA_LICENSE.md); rights holders can use the takedown issue template — removal
  first, discussion later, no legal paperwork required.
- How it was built: the author directed the work, and an AI coding assistant, DeepSeek Harness with
  DeepSeek v4.1-Flash, helped with implementation, debugging, tests and documentation. Data decisions
  and releases are the author's.
- Feedback is welcome, whether it is a wrong fact, a dead source, a bug, or just "this reads oddly".
  Please open an issue; a vague report is still more useful than silently leaving.

## Known limitations

- Chinese-only data and retrieval (see above).
- The knowledge base is a snapshot, as accurate as its `generated_at` date. Game content changes with
  every patch, so the snapshot ages, and individual sources may go stale or disappear. A dead source
  fails loudly and says why; it never fabricates an answer.
- Answers are only as good as the model you configure. The app does not ship or proxy a model.
- Windows-only, and the native window depends on the system WebView2 runtime. There is no macOS/Linux
  path anywhere in the code (DPAPI key storage, WebView2 window, PowerShell packaging) and no plan for
  one.
- Web enrichment depends on third-party sites. The scraper honours `robots.txt`, paces requests and
  fails closed; a captcha, a redesign or an IP block means "no answer", not a made-up one.
- Single maintainer, so no schedule for fixes or features. An issue with reproduction steps gets
  attention fastest.
- No OCR, no image understanding: text pages only.

## License

MIT for the code. Third-party content and dependencies: `DATA_LICENSE.md`,
`THIRD_PARTY_NOTICES.md`. *Neverness to Everness* names and assets belong to their respective
owners; this is an unofficial fan-made tool.
