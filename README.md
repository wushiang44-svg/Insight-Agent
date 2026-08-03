# VOC Insight Agent

Given a product category (e.g. "wireless earbuds"), the agent mines real customer feedback ("Voice of
Customer") from a pluggable data source — Reddit, Amazon reviews, YouTube comments, or a JSON upload —
into a merchant-readable report: what customers care about, what's most important, and a short list of
evidence-backed suggestions. It is deliberately **not** an AI business consultant: recommendations stay
short, conservative, and traceable back to a specific aggregate of collected feedback — never invented
pricing, ROI, cost, or market strategy the data can't support (see "Report reliability" below).

## Pipeline

A ReAct loop repeats search → analyze → sufficiency-check until there's enough evidence (or the iteration
cap / two rounds with no new evidence is hit), then a batch categorization + report stage runs once:

1. **Thought**: an LLM (or a rule-based fallback) decides what to search next, given what's been found so
   far and which aspects still look under-covered.
2. **Action**: search the data source (pluggable, see below).
3. **Screening**: each new item is classified evidence-worthy or not (spam/off-topic/too-thin-to-carry-
   signal is discarded; everything else proceeds — a mixed review isn't discarded just because it also
   contains noise).
4. **Claim extraction**: every evidence-worthy item is broken into atomic, typed claims (problem, feature
   request, praise, comparison, shipping issue, seller/service issue), each with its own sentiment,
   confidence, and a short source excerpt — a single long review yields multiple independent claims
   instead of one lossy "aspect" tag, with near-duplicate claims from the same review merged.
5. **Sufficiency check**: if there isn't enough evidence yet, loop back to step 1.
6. **Categorization**: once the loop ends, every claim collected this run is matched against a
   product-category-scoped canonical taxonomy (lexical match first, LLM fallback, new categories proposed
   rather than silently invented) — this is what turns "floor damage" and "floor_damage" into one topic
   instead of two fragmented ones in the report.
7. **Report generation**: claims are aggregated by (claim type, canonical category) and turned into a
   merchant-readable report — in English and Simplified Chinese side by side (see "Language" below).

The frontend (React + Vite) shows the agent's reasoning process live and lets you view the report once
it's done, plus a separate page for reviewing/approving the categorization taxonomy.

### Report reliability

The report-generation prompt is deliberately constrained, based on empirical review of real generated
reports:

- **No evidence, no report.** If a run collected nothing to summarize, the LLM is never called — the
  agent returns an honest "not enough evidence was collected" message instead of confidently inventing
  one. (Earlier versions of the prompt would fabricate a plausible-sounding report from the model's own
  general knowledge when handed empty input; this is now a hard code-level guard, not a prompt request.)
- **Grounded only.** The prompt instructs the model to use only the supplied aggregate labels, counts,
  sentiment breakdowns, and example quotes — not general knowledge about the product category, the
  company, or competitors.
- **No invented business/technical claims.** ROI, revenue impact, pricing strategy, implementation or
  manufacturing cost, market/business strategy, product positioning, competitor strategy, and any numeric
  measurement/percentage/duration/capacity/frequency/threshold not present in the supplied data are all
  explicitly prohibited — not just as a fixed list of examples, but as a general rule.
- **Every recommendation must cite its evidence.** Each entry in `recommended_actions` must name the exact
  aggregate label (the specific pain point / feature request / praised aspect) it addresses, so it can
  always be matched back to a real input entry rather than a paraphrase or invention.

Without a configured DeepSeek key, every stage above (planning, screening, claim extraction,
categorization, sufficiency-checking, summarizing) falls back to deterministic keyword-rule logic instead
— useful for local development/testing/demos, but noticeably lower quality than a real LLM.

### The data-collection layer is pluggable

`react_agent.py` only depends on the abstract `Collector` interface defined in `app/collectors/base.py`
(`available()` + `search(query, subreddit, limit)`) — it has no knowledge of, and never imports, any
concrete data source. The ReAct loop and downstream claim-extraction/categorization/report logic are
identical regardless of which collector produced the evidence.

```
app/collectors/
  base.py             Collector abstract interface + CollectorContext
  registry.py         DataSource -> factory registry; build_collector() is the only lookup point
  reddit_browser.py   RedditBrowserCollector — the primary Reddit source (see below)
  _reddit_chrome.py   CDP lifecycle helper for reddit_browser.py's dedicated Chrome instance
  reddit.py           RedditCollector (PRAW, read-only OAuth) — legacy, kept for old runs only
  scraper.py          RedditScraperCollector (unofficial HTTP scrape) — legacy, kept for old runs only
  json_upload.py      JsonUploadCollector (offline/demo source)
  amazon.py           AmazonCollector (product reviews via browser automation)
  youtube.py          YoutubeCollector (video comments via browser automation)
  _agent_browser.py   Shared subprocess wrapper around the `agent-browser` CLI, used by amazon.py/youtube.py
```

Four sources are offered for new runs today:

- **Reddit** (`app/collectors/reddit_browser.py`, `DataSource.REDDIT`): the primary Reddit source. Drives
  a plain, independently-launched Chrome instance that the collector attaches to over the Chrome DevTools
  Protocol (not `agent-browser`'s own managed-browser mode), warmed up by exactly one manual human visit —
  no Reddit login is required, the persisted profile just needs to carry ordinary browsing trust past
  Reddit's bot detection. Reddit's Data API now requires approval under its "Responsible Builder" policy
  and is unreliable to depend on, which is why this became the primary path instead.
- **Amazon Reviews** (`app/collectors/amazon.py`): Amazon has no public reviews API, so this drives a
  real, logged-in Chrome session via the [agent-browser](https://github.com/vercel-labs/agent-browser)
  CLI. Requires a one-time manual login into a persistent agent-browser profile — logged-out sessions only
  see a truncated AI summary, not individual reviews. Reviews are fetched by sweeping all five star-rating
  filter pages per product and merging them round-robin, since Amazon's own "load more" pagination
  reliably fails under automation.
- **YouTube Comments** (`app/collectors/youtube.py`): also browser automation via agent-browser, but no
  login needed since comments are public. Searches videos, then scrolls each one's comment section to
  trigger lazy-loading.
- **JSON upload** (`app/collectors/json_upload.py`): feeds a pre-prepared JSON array of posts/comments to
  the agent. Requires no credentials or browser automation at all — good for demos, offline analysis, or
  exercising the full pipeline without any of the above set up.

Two older Reddit collectors (`reddit.py`, PRAW-based; `scraper.py`, unofficial HTTP scraping) still exist
and are still registered, purely so runs created before `reddit_browser.py` existed keep resolving to their
original collector — they are no longer offered as a data-source choice for new runs.

Pick the data source when creating a run in the frontend; all four share the exact same downstream
screening/claim-extraction/categorization/report logic. Customer review/comment text is never rewritten or
translated by the pipeline — it's evidence, and every quote in the report links back to its source.

### Claims taxonomy & curation

Claims are matched against a `product_category`-scoped canonical taxonomy rather than grouped by raw,
free-text aspect strings — this is what keeps the report from splitting one real issue into several
differently-worded near-duplicate entries. New categories a run's claims don't lexically match anything
existing are proposed, not silently created as ground truth; the `/taxonomy` page in the frontend lets a
human approve, rename, merge, or deprecate categories, with every transition recorded in an audit log
(`GET /categories/{id}/history`). A run's report only takes this categorized-Claims path once categorization
completed with enough resolved coverage (a configurable minimum ratio); otherwise it falls back to the
original evidence-aspect grouping, and which path (and why, if it fell back) is recorded on the report
itself.

### Language

The report is generated in English and Simplified Chinese in the same DeepSeek call (not translated
afterward) — `Report.summary_markdown`/`recommended_actions` (English) and
`Report.summary_markdown_zh`/`recommended_actions_zh` (Chinese) are both stored, and the frontend's
EN/中文 switcher (top right, persisted to `localStorage`) picks between them with no extra request. Without
a DeepSeek key, the deterministic fallback narrative has a matching hardcoded Chinese template. The rest of
the UI (labels, buttons, aspect names for a curated common-word list) is translated client-side via
`frontend/src/lib/i18n.tsx`; direct customer quotes are always left as-is.

## Project layout

```
backend/    FastAPI + SQLite + ReAct agent + claims/taxonomy pipeline (Python)
frontend/   Vite + React + TypeScript
docs/       Phase-by-phase architecture/validation plans (claims taxonomy, merchant report reliability, ...)
```

## Setup

### 1. DeepSeek API key (optional, but strongly recommended)

Get one at https://platform.deepseek.com. Only **one** API key is needed — like most LLM providers,
DeepSeek authenticates a whole account with a single key, and the model is picked per API request, not per
key. The app still runs without a key — it falls back to deterministic keyword-rule logic (noticeably
lower quality) at every LLM-backed stage.

The two model environment variables (see `.env.example`) split work by cost/quality:

- `FAST_MODEL` (default `deepseek-v4-flash`): search planning, screening, claim extraction, categorization,
  and sufficiency checks — many small calls per run, so a cheap/fast model makes sense here.
- `PRO_MODEL` (default `deepseek-v4-pro`): only the final merchant report — one call per run, where output
  quality matters most.

Double-check these default model names against DeepSeek's current docs/dashboard before relying on them —
they may not match the exact model IDs available on your account.

### 2. Reddit browser collector (optional — not needed if you only use Amazon/YouTube/JSON upload)

No Reddit account or API credentials needed, just a one-time manual warm-up of a dedicated Chrome profile:

```bash
"<path-to-chrome.exe>" --remote-debugging-port=9222 --user-data-dir="<profile-dir-of-your-choice>"
```

Manually browse to reddit.com, open one thread with comments, confirm it loads normally (no challenge/block
page), then close the window — this only needs to happen once per profile. Point
`REDDIT_CHROME_PROFILE_DIR` at that same path in `.env` (`REDDIT_CHROME_EXECUTABLE`/`REDDIT_CHROME_CDP_PORT`
only need to be set if you're not using Chrome's default install location or port 9222).

### 3. Amazon / YouTube collectors (optional — not needed if you only use Reddit/JSON upload)

Both drive a real Chrome session via the [agent-browser](https://github.com/vercel-labs/agent-browser) CLI:

```bash
npm install -g agent-browser
agent-browser install   # downloads a Chrome for Testing runtime, first time only
```

YouTube needs nothing else — comments are public. Amazon needs a one-time manual login into a persistent
profile before it can read full reviews:

```bash
agent-browser --profile "<path-to-a-profile-dir>" open https://www.amazon.com --headed
# log in by hand in the window that opens, then close it — the login persists to that profile directory
```

Point `AMAZON_AGENT_BROWSER_PROFILE` (see `.env` below) at that same path.

### 4. Configure `.env`

```bash
cd backend
cp .env.example .env
# edit .env: fill in DEEPSEEK_API_KEY (FAST_MODEL/PRO_MODEL have defaults) /
# REDDIT_CHROME_PROFILE_DIR / AMAZON_AGENT_BROWSER_PROFILE
# if you only plan to use JSON upload mode, you can skip this entirely
```

## Run the backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/pip install -e .          # Windows
# source .venv/bin/activate && pip install -e .   # macOS/Linux
./.venv/Scripts/python -m uvicorn app.main:app --reload
```

The backend listens on `http://127.0.0.1:8000` by default.

Run the tests (no API key needed — they inject fake collector/LLM clients):

```bash
./.venv/Scripts/python -m pytest
```

## Run the frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Usage

1. On the home page, click "New Run" and fill in the product category (required), keywords, target
   subreddits (Reddit only), max iterations, and target evidence count.
2. Choose a "Data source":
   - **Reddit**: requires the one-time Chrome profile warm-up described in Setup. If it's not configured,
     the page falls back to JSON upload by default and shows a warning if you select Reddit anyway.
   - **Amazon Reviews**: requires `agent-browser` installed and a one-time login (see Setup). If either is
     missing, the page shows a warning.
   - **YouTube Comments**: requires `agent-browser` installed; no login needed.
   - **JSON upload**: upload a JSON array of posts/comments (a format example is shown on the page). No
     credentials required.
3. After submitting, you land on the run detail page, which polls the backend every 2 seconds and shows
   the agent's thought / search / screening / claim-extraction / sufficiency-check steps live.
4. Once the status becomes "Completed", click "View merchant report" to see pain points, feature requests,
   praise, competitor mentions, shipping/service issues, and sentiment breakdown grouped by canonical
   category, plus a short list of evidence-backed recommended actions — switch EN/中文 top right at any
   time.
5. Visit "Taxonomy" in the top nav to review categories proposed by recent runs, approve or rename them,
   merge duplicates, or deprecate ones that no longer make sense — every change is recorded in that
   category's audit history.

## Notes

- If "Reddit" is selected but the Chrome profile isn't warmed up yet, the search action fails with a clear
  one-time-setup message recorded in the reasoning trace (it won't crash the whole run) — the agent still
  runs to the iteration cap and generates a report based on 0 pieces of evidence, useful for verifying the
  pipeline end-to-end but not meaningful otherwise. To see real content, either complete the Reddit Setup
  step or switch to "JSON upload".
- In "JSON upload" mode, the agent never returns the same item twice; once the uploaded data is exhausted,
  it's treated as "two rounds with no new evidence" and the loop moves to the report stage automatically —
  this is also the simplest, most reliable way to exercise the ReAct loop's judgment logic.
- Without a configured DeepSeek key, planning / screening / claim extraction / categorization /
  sufficiency-checking / summarizing all fall back to deterministic keyword rules — useful for local
  development and testing, but report quality is nowhere near as good as with a real LLM.
- Amazon/YouTube collectors drive a real browser, so they're inherently slower than an API and depend on
  the target site's current page structure and anti-automation behavior — both have been observed to
  throttle or block a session that navigates too aggressively (Amazon's own "load more" pagination, and
  YouTube's comment lazy-load, have both failed under heavy back-to-back automated use in testing). If a
  run comes back with far less evidence than expected, this is the first thing to suspect; pace requests
  and retry later rather than assuming the collector code is broken.
- A run's report only uses the categorized-Claims path once categorization has run and resolved a
  sufficient share of that run's claims; otherwise it automatically falls back to grouping by raw evidence
  aspect instead, and the report records which path was used and why.
