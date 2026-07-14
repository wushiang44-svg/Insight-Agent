# VOC Insight Agent

Given a product category (e.g. "wireless earbuds"), the agent runs a ReAct loop against a pluggable data source — Reddit, Amazon reviews, YouTube comments, or a JSON upload — to mine real customer feedback ("Voice of Customer") into a merchant-readable report:

1. **Thought**: decide what to search next.
2. **Action**: search (the data source is pluggable, see below).
3. **Observation**: use an LLM (or a rule-based fallback) to filter for genuinely relevant posts/comments/reviews and tag them as pain points, feature requests, competitor comparisons, or praise.
4. **Sufficiency check**: if there isn't enough evidence yet, loop back to step 1; if there's enough (or the iteration cap is reached, or two rounds in a row found no new evidence), move to the summary stage.
5. **Summary**: generate a merchant-readable product improvement report — in English and Simplified Chinese side by side (see "Language" below).

The frontend (React + Vite) shows the agent's reasoning process live and lets you view the report once it's done.

The architecture was inspired by `demo/super_crawler` (a more complete Reddit requirement-discovery system), simplified down to a single ReAct loop, with a standalone React frontend + FastAPI backend. Reddit was the original and only source; Amazon and YouTube were added later as additional `Collector` implementations without touching the ReAct loop itself (see below).

### The data-collection layer is pluggable

`react_agent.py` only depends on the abstract `Collector` interface defined in `app/collectors/base.py` (`available()` + `search(query, subreddit, limit)`) — it has no knowledge of, and never imports, any concrete data source. Whether the source is Reddit (PRAW), Amazon, YouTube, or a JSON upload makes no difference to the ReAct loop or the downstream analysis/scoring/summary logic.

```
app/collectors/
  base.py            Collector abstract interface + CollectorContext
  registry.py        DataSource -> factory registry; build_collector() is the only lookup point
  reddit.py          RedditCollector (PRAW, read-only OAuth mode), self-registers via register_collector(...) at the bottom of the file
  scraper.py         RedditScraperCollector (unofficial stopgap, no OAuth), self-registers the same way
  json_upload.py     JsonUploadCollector (offline/demo source), self-registers the same way
  amazon.py          AmazonCollector (product reviews via browser automation), self-registers the same way
  youtube.py         YoutubeCollector (video comments via browser automation), self-registers the same way
  _agent_browser.py  Shared subprocess wrapper around the `agent-browser` CLI, used by amazon.py and youtube.py
```

When starting a run, `run_manager.py` only ever calls `build_collector(CollectorContext(run, storage))` — it looks up the factory in the registry and calls it, and never branches on `data_source` itself. That means adding another review platform later is just:

1. Add a new enum value to `DataSource` in `models.py`.
2. Create `app/collectors/<name>.py`, write a class implementing the `Collector` interface, and call `register_collector(DataSource.<NAME>, ...)` at the bottom of the file to self-register.
3. Add one import line in `app/collectors/__init__.py`: `from . import <name> as _<name>`.

`react_agent.py` and `run_manager.py` never need to change.

Five data sources exist today:

- **Reddit API** (`app/collectors/reddit.py`): live search via PRAW's read-only OAuth mode. **The Reddit Data API currently requires approval under the new "Responsible Builder" policy, so applications may be rejected or take a long time to process**, meaning this path may be temporarily unavailable.
- **Reddit scraper** (`app/collectors/scraper.py`): hits Reddit's public `.json` listing/search/comment endpoints directly over HTTP, with no OAuth credentials required. It's unofficial and unsupported — no SLA, much more aggressive rate limiting/blocking than the real API, and technically outside Reddit's API Terms for automated collection — so treat it as a temporary bridge while a Reddit API application is pending, not a long-term replacement. Client-side requests are throttled (2s delay by default) to be polite to the unauthenticated endpoint.
- **Amazon Reviews** (`app/collectors/amazon.py`): Amazon has no public reviews API, so this drives a real, logged-in Chrome session via the [agent-browser](https://github.com/vercel-labs/agent-browser) CLI instead. Requires a one-time manual login into a persistent agent-browser profile (see the collector's docstring) — logged-out sessions only see a truncated AI summary, not individual reviews. Reviews are fetched by sweeping all five star-rating filter pages per product and merging them round-robin, since Amazon's own "load more" pagination reliably fails under automation.
- **YouTube Comments** (`app/collectors/youtube.py`): also browser automation via agent-browser, but no login needed since comments are public. Searches videos, then scrolls each one's comment section to trigger lazy-loading.
- **JSON upload** (`app/collectors/json_upload.py`): feeds a pre-prepared JSON array of posts/comments to the agent. Requires no credentials or browser automation at all — good for demos, offline analysis, or exercising the full pipeline without any of the above set up.

Pick the "data source" when creating a run in the frontend; all five share the exact same downstream analysis/judgment/summary logic. Customer review/comment text is never rewritten or translated by the pipeline — it's evidence, and every quote in the report links back to its source.

### Language

The report is generated in English and Simplified Chinese in the same DeepSeek call (not translated afterward) — `Report.summary_markdown`/`recommended_actions` (English) and `Report.summary_markdown_zh`/`recommended_actions_zh` (Chinese) are both stored, and the frontend's EN/中文 switcher (top right, persisted to `localStorage`) picks between them with no extra request. Without a DeepSeek key, the deterministic fallback narrative has a matching hardcoded Chinese template. The rest of the UI (labels, buttons, aspect names for a curated common-word list) is translated client-side via `frontend/src/lib/i18n.tsx`; direct customer quotes are always left as-is.

## Project layout

```
backend/    FastAPI + SQLite + ReAct agent (Python)
frontend/   Vite + React + TypeScript
```

## Setup

### 1. Reddit API credentials (optional — not needed if you pick "JSON upload")

As of the "Responsible Builder" policy, Reddit Data API access is no longer fully self-service — you first need
to request access/approval:

1. Submit a request at https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164
   (requires logging into your Reddit account; the form asks about your intended use case).
2. Once approved, create/access your app's credentials at https://www.reddit.com/prefs/apps ("create app" /
   "create another app", type **script**).
3. Note down `client_id` (the string under the app name) and `client_secret` — these are still required
   regardless of the approval process, since PRAW authenticates with them directly.

If you can't get approved right away (see "The data-collection layer is pluggable" above), just skip this step and pick "JSON upload" when creating a run.

### 2. DeepSeek API key (optional, but strongly recommended)

Get one at https://platform.deepseek.com. Only **one** API key is needed — like most LLM providers, DeepSeek
authenticates a whole account with a single key, and the model is picked per API request, not per key. The app
still runs without a key — it falls back to deterministic keyword-rule logic (noticeably lower quality).

The two model environment variables (see `.env.example`) split work by cost/quality:

- `FAST_MODEL` (default `deepseek-v4-flash`): search planning, item relevance/tagging analysis, and sufficiency
  checks — many small calls per run, so a cheap/fast model makes sense here.
- `PRO_MODEL` (default `deepseek-v4-pro`): only the final merchant report — one call per run, where output
  quality matters most.

Double-check these default model names against DeepSeek's current docs/dashboard before relying on them — they
may not match the exact model IDs available on your account.

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
# REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT /
# AMAZON_AGENT_BROWSER_PROFILE
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

1. On the home page, click "New Run" and fill in the product category (required), keywords, target subreddits (Reddit only), max iterations, and target evidence count.
2. Choose a "Data source":
   - **Reddit API**: requires Reddit credentials configured in `.env`. If they're missing, the page shows a warning and suggests switching to the scraper or JSON upload.
   - **Reddit scraper**: no credentials required, but unofficial and rate-limited — see the warning above.
   - **Amazon Reviews**: requires `agent-browser` installed and a one-time login (see Setup). If either is missing, the page shows a warning.
   - **YouTube Comments**: requires `agent-browser` installed; no login needed.
   - **JSON upload**: upload a JSON array of posts/comments (a format example is shown on the page). No credentials required.
3. After submitting, you land on the run detail page, which polls the backend every 2 seconds and shows the agent's thought / search / observation / sufficiency-check steps live.
4. Once the status becomes "Completed", click "View merchant report" to see pain points, feature requests, praise, competitor mentions, and sentiment breakdown grouped by aspect, plus recommended product-improvement actions — switch EN/中文 top right at any time.

## Notes

- If "Reddit API" is selected but no credentials are configured, the search action fails and is recorded in the reasoning trace (it won't crash the whole run) — the agent still runs to the iteration cap and generates a report based on 0 pieces of evidence, useful for verifying the pipeline end-to-end but not meaningful otherwise. To see real content, either configure Reddit credentials or switch to "JSON upload".
- In "JSON upload" mode, the agent never returns the same item twice; once the uploaded data is exhausted, it's treated as "two rounds with no new evidence" and the loop moves to the summary stage automatically — this is also the simplest, most reliable way to exercise the ReAct loop's judgment logic.
- Without a configured DeepSeek key, planning / analysis / sufficiency-checking / summarizing all fall back to deterministic keyword rules — useful for local development and testing, but report quality is nowhere near as good as with a real LLM.
- Amazon/YouTube collectors drive a real browser, so they're inherently slower than an API and depend on the target site's current page structure and anti-automation behavior — both have been observed to throttle or block a session that navigates too aggressively (Amazon's own "load more" pagination, and YouTube's comment lazy-load, have both failed under heavy back-to-back automated use in testing). If a run comes back with far less evidence than expected, this is the first thing to suspect; pace requests and retry later rather than assuming the collector code is broken.
