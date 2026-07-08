# Reddit Product Feedback Insight Agent

Given a product category (e.g. "wireless earbuds"), the agent runs a ReAct loop on Reddit:

1. **Thought**: decide what to search next.
2. **Action**: search (the data source is pluggable, see below).
3. **Observation**: use an LLM (or a rule-based fallback) to filter for genuinely relevant posts/comments and tag them as pain points, feature requests, competitor comparisons, or praise.
4. **Sufficiency check**: if there isn't enough evidence yet, loop back to step 1; if there's enough (or the iteration cap is reached, or two rounds in a row found no new evidence), move to the summary stage.
5. **Summary**: generate a merchant-readable product improvement report.

The frontend (React + Vite) shows the agent's reasoning process live and lets you view the report once it's done.

The architecture was inspired by `demo/super_crawler` (a more complete Reddit requirement-discovery system), simplified down to a single ReAct loop, with a standalone React frontend + FastAPI backend.

### The data-collection layer is pluggable

`react_agent.py` only depends on the abstract `Collector` interface defined in `app/collectors/base.py` (`available()` + `search(query, subreddit, limit)`) — it has no knowledge of, and never imports, any concrete data source. Whether the source is Reddit (PRAW), a JSON upload, or a future Amazon/YouTube collector makes no difference to the ReAct loop or the downstream analysis/scoring/summary logic.

```
app/collectors/
  base.py          Collector abstract interface + CollectorContext
  registry.py       DataSource -> factory registry; build_collector() is the only lookup point
  reddit.py         RedditCollector (primary source, PRAW, read-only mode), self-registers via register_collector(...) at the bottom of the file
  json_upload.py    JsonUploadCollector (fallback source), self-registers the same way
```

When starting a run, `run_manager.py` only ever calls `build_collector(CollectorContext(run, storage))` — it looks up the factory in the registry and calls it, and never branches on `data_source` itself. That means adding Amazon, YouTube, or another review platform later is just:

1. Add a new enum value to `DataSource` in `models.py`.
2. Create `app/collectors/amazon.py`, write a class implementing the `Collector` interface, and call `register_collector(DataSource.AMAZON, ...)` at the bottom of the file to self-register.
3. Add one import line in `app/collectors/__init__.py`: `from . import amazon as _amazon`.

`react_agent.py` and `run_manager.py` never need to change.

Two data sources exist today:

- **Reddit API** (primary source, `app/collectors/reddit.py`): live search. **The Reddit Data API currently requires approval under the new "Responsible Builder" policy, so applications may be rejected or take a long time to process**, meaning this path may be temporarily unavailable.
- **JSON upload** (fallback source, `app/collectors/json_upload.py`): feeds a pre-prepared JSON array of Reddit posts/comments to the agent. Requires no Reddit credentials at all — good for demos, offline analysis, or getting the full pipeline working before a Reddit API application comes through.

Pick the "data source" when creating a run in the frontend; both share the exact same downstream analysis/judgment/summary logic.

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

Get one at https://platform.deepseek.com. The app still runs without it — it falls back to deterministic keyword-rule logic (noticeably lower quality).

### 3. Configure `.env`

```bash
cd backend
cp .env.example .env
# edit .env: fill in DEEPSEEK_API_KEY / REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT
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

1. On the home page, click "New Run" and fill in the product category (required), keywords, target subreddits, max iterations, and target evidence count.
2. Choose a "Data source":
   - **Reddit API**: requires Reddit credentials configured in `.env`. If they're missing, the page shows a warning and suggests switching to JSON upload.
   - **JSON upload**: upload a JSON array of Reddit posts/comments (a format example is shown on the page). No Reddit credentials required.
3. After submitting, you land on the run detail page, which polls the backend every 2 seconds and shows the agent's thought / search / observation / sufficiency-check steps live.
4. Once the status becomes "Completed", click "View merchant report" to see pain points, feature requests, praise, competitor mentions, and sentiment breakdown grouped by aspect, plus recommended product-improvement actions.

## Notes

- If "Reddit API" is selected but no credentials are configured, the search action fails and is recorded in the reasoning trace (it won't crash the whole run) — the agent still runs to the iteration cap and generates a report based on 0 pieces of evidence, useful for verifying the pipeline end-to-end but not meaningful otherwise. To see real content, either configure Reddit credentials or switch to "JSON upload".
- In "JSON upload" mode, the agent never returns the same item twice; once the uploaded data is exhausted, it's treated as "two rounds with no new evidence" and the loop moves to the summary stage automatically — this is also the simplest, most reliable way to exercise the ReAct loop's judgment logic.
- Without a configured DeepSeek key, planning / analysis / sufficiency-checking / summarizing all fall back to deterministic keyword rules — useful for local development and testing, but report quality is nowhere near as good as with a real LLM.
