# VOC Insight Agent — Pipeline Overview (for evaluation)

This document briefs an evaluator (human or agent) who has no prior context on this codebase. It describes the end-to-end pipeline, the architecture decisions behind it, and — deliberately — the known gaps and unverified parts, so an evaluation isn't just reading the happy path.

## One-line description

Given a product category (e.g. "wireless earbuds"), the agent runs a ReAct loop against a pluggable data source (Reddit / Amazon / YouTube / a local JSON upload) to mine scattered user feedback into a structured, merchant-readable report — pain points, feature requests, competitor mentions, praise, sentiment breakdown, priority ranking, and a recommended-fixes roadmap. The report is generated bilingually (English + Simplified Chinese) in the same LLM call.

## High-level architecture

```
+-------------+     HTTP/REST      +-------------------+
|  React UI   | <----------------> |  FastAPI backend   |
| (Vite + TS) |   polls every 2s   |    routes.py        |
+-------------+                    +---------+----------+
                                             |
                                    +--------v----------+
                                    |   RunManager       |
                                    | (one background    |
                                    |  thread per run)    |
                                    +--------+----------+
                                             |
                              +--------------v----------------+
                              |   react_agent.run_react_loop   |
                              | Thought -> Action -> Observation|
                              | -> Sufficiency -> ... -> Summary|
                              +---+-----------------------+----+
                                  |                        |
                        +---------v--------+     +---------v--------+
                        | Collector         |     | DeepSeekClient    |
                        | (pluggable):       |     | (LLM, with a      |
                        | Reddit/Amazon/     |     | rule-based        |
                        | YouTube/JSON       |     | fallback)          |
                        +-------------------+     +-------------------+
                                  |
                        +---------v--------+
                        | SQLite (storage) |
                        | runs / evidence /|
                        | trace_events /   |
                        | reports          |
                        +------------------+
```

## End-to-end flow

1. **Create a run** (`POST /runs`, `routes.py`): the frontend submits a product category, keywords, a `DataSource` (`reddit_api` | `reddit_scraper` | `amazon` | `youtube` | `json_upload`), max iterations, and a target evidence count. `storage.create_run()` persists it; `RunManager.start_run()` spawns a background thread to execute it; the `run_id` is returned immediately.

2. **Build a Collector** (`collectors/registry.py`): `run_manager.py` calls `build_collector(CollectorContext(run, storage))`, which looks up a factory by `run.data_source` in a registry — it never branches on the data source itself. Each collector module self-registers via `register_collector(DataSource.X, factory)` at import time, so adding a new source is one new file plus one import line; `react_agent.py` never has to change.

3. **The ReAct loop** (`react_agent.run_react_loop`, per iteration):
   - **Thought** (`plan_next_query`): an LLM (or a rule-based fallback) decides the next search query and a "group" hint (Reddit's subreddit concept — see Known Gaps below for why this doesn't generalize cleanly to other sources).
   - **Action** (`collector.search(query, group, limit)`): calls the concrete collector, returns a batch of `CollectedItem` (title/body/source URL/group label/timestamp/score — a source-agnostic shape).
   - **Observation** (`analyze_item`, per item): an LLM (or rules) filters for relevance and tags `insight_type` (pain_point/feature_request/comparison/praise/question/noise), a free-text `aspect` label (e.g. "battery", "comfort" — not a fixed enum), `sentiment`, and a confidence score; relevant items are persisted as `Evidence`.
   - **Sufficiency check**: looks at evidence volume, source diversity, and aspect coverage. Stops when sufficient, when the iteration cap is hit, or after two consecutive iterations with zero new evidence ("diminishing returns"); otherwise loops back to Thought with a hint about which aspects are under-covered.
   - **Summary**: `summarize()` aggregates all `Evidence` by `aspect` (`_aggregate_by_aspect`), then makes one LLM call (the "pro" model) that writes the narrative report (summary + recommended actions) in English AND Simplified Chinese simultaneously, storing both on `Report`.

4. **Frontend polling** (`RunDetail.tsx`, every 2s): live-renders each round's Thought/Search/Results/Filtering/Decision. On completion, links to the report page (`Report.tsx`) — KPI cards, a sentiment donut, a priority-ranked list (a heuristic scoring formula, not a statistically rigorous ranking — it's meant to answer "what to fix first," not to be audited), evidence grouped by aspect with linked quotes, and a fix/build roadmap.

## The pluggable data-source layer (5 sources today)

| Source | Mechanism | Credentials/login | Known limitations |
|---|---|---|---|
| `reddit_api` | PRAW, read-only OAuth | Requires Reddit Data API approval (pending, unresolved as of this writing) | Not currently usable in practice |
| `reddit_scraper` | Direct HTTP to Reddit's public `.json` endpoints | None | Unofficial; more aggressive rate limiting/blocking than the real API |
| `json_upload` | Reads a user-uploaded JSON array | None | Offline/demo only, not live data |
| `amazon` | Drives a real, logged-in Chrome session via the `agent-browser` CLI; sweeps all 5 star-rating filter pages per product and round-robin merges them | One-time manual login into a persistent browser profile | Effective yield capped at roughly 30-50 reviews/product after dedup; a "Verified Purchase" signal is captured but has never actually been exercised against a real unverified review in testing |
| `youtube` | `agent-browser` searches videos, then scrolls each one's comment section to trigger lazy-loading | None (comments are public) | **As of the last test, comment loading was actively throttled/broken** on the test machine after heavy automated use — the extraction logic itself was validated against real loaded data earlier, but a full end-to-end run was never successfully completed since |

Amazon and YouTube share low-level browser-automation plumbing in `collectors/_agent_browser.py`, which works around several Windows-specific `subprocess`/Chrome-automation pitfalls (a `.cmd` shim that can't run without `shell=True`; a background daemon that inherits pipe handles and hangs `subprocess.run` past its own timeout; transient `WinError 32` file-sharing violations; GBK-vs-UTF-8 console decoding). Each collector instance is pinned to an `agent-browser` `--session` keyed by `run_id`, because concurrent runs sharing the default session would silently read each other's browser tab.

## The LLM layer

`llm.py`'s `DeepSeekClient` splits work by cost/quality: a fast/cheap model for the many small per-item planning/analysis/sufficiency calls, and a "pro" model for the one big final-report call. **With no API key configured, the entire pipeline degrades to deterministic keyword/regex rules** (`text.py`) — noticeably lower quality, but the system stays fully exercisable end-to-end without any LLM access, which is a deliberate design choice for testability.

## Data model (`models.py`)

`RunRecord` (task metadata + status) -> `CollectedItem` (a collector's raw, source-agnostic output) -> `Evidence` (an analyzed, tagged, persisted item — note: the field is still named `subreddit` for historical reasons, but it's documented as a generic "grouping label"; for Amazon it holds a product name) -> `TraceEvent` (per-round-per-stage log entries the frontend timeline renders from) -> `Report` (final output, with parallel English/Chinese narrative fields).

## Frontend (`frontend/src/`)

Four pages (`RunsList`/`CreateRun`/`RunDetail`/`Report`) plus:
- `lib/i18n.tsx`: a dictionary-based EN/ZH translation layer (React Context, localStorage-persisted) covering all static UI text.
- `lib/sources.ts`: a `useSourceMeta()` hook that derives source-aware vocabulary (Reddit -> "subreddits"/"r/" prefix, Amazon -> "products", YouTube -> "videos") so a non-Reddit report doesn't show mismatched Reddit terminology.
- `lib/aspectTranslations.ts`: a ~35-term dictionary translating common pain-point/praise aspect labels into Chinese; aspects are LLM free-text (not an enum), so anything unmapped falls back to the English original rather than showing blank.

Customer review/comment quotes are never translated or reworded anywhere in the pipeline — only the AI-generated summary/recommendations have a Chinese counterpart.

## Known gaps (worth an evaluator's attention)

1. **The search-planner prompt is still Reddit-specific.** `plan_next_query`'s system prompt literally frames the LLM as "a Reddit search-planning agent," regardless of the run's actual `data_source`. Query quality for Amazon/YouTube runs has not been specifically tuned or validated against this mismatch.
2. **The YouTube collector's end-to-end path is unverified as of the last session** (see table above) — the extraction code is correct against real data captured earlier, but no complete real run has confirmed it since the throttling was observed.
3. **The Amazon "Verified Purchase" authenticity signal has only ever seen positive cases** in testing — never a genuine unverified review — so its practical value as a fake-review filter is unproven, not just untuned.
4. **Amazon/YouTube collection is slow and not fully predictable**, being real-browser-driven and deliberately rate-limited; unlike an API, throughput and reliability depend on the target site's current anti-automation posture, which has visibly changed mid-session during development.
5. **The `subreddit` field name throughout `CollectedItem`/`Evidence`/the database schema is a historical artifact** — functionally a generic grouping label now, but the name itself is misleading to anyone reading the schema cold.
6. The GitHub repository is still named `Insight-Agent` (the local project and its documentation were renamed to "VOC Insight Agent"; the GitHub rename is a manual step not yet done).

## Where to start reading the code

- `backend/app/react_agent.py` — the ReAct loop itself, start here.
- `backend/app/collectors/base.py` — the abstract interface every data source implements.
- `backend/app/collectors/amazon.py` / `youtube.py` — the most recently added, most complex collectors (heavy inline comments explaining non-obvious workarounds).
- `backend/app/models.py` — every data shape in the system.
- `frontend/src/pages/Report.tsx` — the most complex frontend page; a good tour of how a `Report` becomes a UI.
