# Production Reddit Browser Collector — Integration Plan

## Context

`RedditScraperCollector` (the unofficial `.json`-endpoint path) now returns a hard HTTP 403 in this
environment, and a long series of live, real-network experiments (documented across this session)
established that:

- A browser session **launched and driven directly by agent-browser** (its own managed Chrome, even
  with a persistent `--profile`) reliably hits Reddit's `js_challenge` wall on first navigation.
- A **plain, independently-launched Chrome process** (`chrome.exe --remote-debugging-port=... --user-data-dir=<dedicated profile>`),
  warmed up by exactly one manual human visit, then **attached to (not launched by) agent-browser via `--cdp`**,
  reliably passes — and that trust **survived two independent full restarts**, with zero manual
  interaction required after the one-time setup.
- Two end-to-end POCs (mechanical keyboards: 3/3 posts, 73 unique comments; wireless headphones: 2/2
  posts, 45 unique comments) validated search → relevance selection → navigation → comment extraction,
  entirely via structured Reddit DOM attributes (no fragile page-text scraping needed for most fields).
- `agent-browser`'s CDP `close` command **lied** about having terminated the browser process in one
  test — verified independently via OS process inspection. Any production shutdown logic must never
  trust that signal alone.

This plan turns that validated architecture into a real `Collector` implementation, without touching
Phase 1/Phase 2 pipeline logic, without Playwright (no capability gap identified — agent-browser's
`--cdp` attach mode already covers every operation used), and without attempting to remove the
one-time manual profile initialization step.

## Audit summary (files read this session)

- `collectors/base.py` — `Collector` Protocol (`available()`, `search()`) and `CollectorContext`. Thin,
  stable interface; new collector just needs to satisfy it.
- `collectors/registry.py` — a strict **1:1 `DataSource → factory` map**. Only one collector is ever
  built per run (resolved from `run.data_source`). There is **no runtime fallback/precedence concept**
  and none is needed — two Reddit collectors can never run simultaneously for the same run, so "silent
  duplication" isn't structurally possible; the real question is just which `DataSource` a run picks.
- `collectors/reddit.py` (`DataSource.REDDIT_API`, PRAW/OAuth) and `collectors/scraper.py`
  (`DataSource.REDDIT_SCRAPER`, unofficial `.json`, currently 403) — both untouched, both fine to leave exactly as-is.
- `collectors/_agent_browser.py` — the shared subprocess wrapper (`AgentBrowserSession`, `parse_eval_json`,
  native-binary resolution, temp-file I/O to dodge the documented Windows pipe-hang). Reused as-is for the
  actual `eval`/`click` calls once attached; **not** used in its `open()`/launch-a-browser role for Reddit.
- `collectors/amazon.py` — persistent-profile pattern (`_default_profile()` env-var + home-dir default,
  one-time manual login instructions in a `RuntimeError`, `available()` checks profile-dir existence).
  Directly reusable *pattern*, but Reddit's trust model is different in kind: Amazon's profile persists a
  **login**; Reddit's profile persists **behavioral/session trust with no login at all** — worth stating
  explicitly so the new code isn't mistaken for needing credentials.
- `collectors/youtube.py` — the ephemeral (`profile=None`) `AgentBrowserSession` pattern, scroll-based
  extraction. Confirms `AgentBrowserSession` already supports both persistent and ephemeral modes.
- `models.py` — `CollectedItem` (10 fields, all confirmed sufficient for Reddit DOM data — see §6) and
  `DataSource(StrEnum)`, whose own docstring says: *"Add one entry here per new collector... nothing
  else needs to change."*
- `react_agent.py` — the exact integration point: `collector.search(thought["query"], subreddit=..., limit=25)`
  at line 108, already wrapped in try/except (a failing search traces `"Search failed: {exc}"` and
  continues with `items=[]` — **the whole graceful-degradation contract this plan needs already exists**).
  `screen_item()`/`extract_claims()` run *after* collection, in the loop, never inside a collector —
  confirms collect/decide separation is already the house style, not something new to invent.
- `.env.example` / `.gitignore` — config-documentation convention (Amazon-style comment blocks); `.env`
  already gitignored; existing profile dirs already live under the user's home directory, outside the
  repo, so they never needed a `.gitignore` entry in the first place.
- `backend/tests/test_scraper_collector.py` — the project's fake-dependency-injection test style
  (`FakeSession` swapped in via constructor). No unit tests exist yet for the two existing agent-browser
  collectors (real-browser dependency), which is why a separate, non-pytest smoke test is the established gap to fill.
- `pyproject.toml` — no `psutil` today; proposing to add it (see §2) specifically for safe process identity.

## 1. REVISED — Reddit as one product-level source, hidden internal implementation

**Re-audit finding that changes this section**: the frontend's `CreateRun.tsx` (`SOURCE_OPTIONS`,
line 9) already exposes `"reddit_api"` **and** `"reddit_scraper"` as two separate user-facing radio
options today. My original plan would have added a third (`reddit_browser`), producing exactly the
"three Reddit choices" the user doesn't want. Revised to collapse toward one.

**New architecture**: add a single new canonical value, `DataSource.REDDIT = "reddit"`. Register it with
one factory that directly builds `RedditBrowserCollector` — the validated, working implementation. This
is still a plain 1:1 entry in the existing `_REGISTRY` dict (`registry.py`'s existing shape is
untouched), so it stays exactly as simple as every other collector — no fallback framework, no runtime
branching, no over-engineering: `DataSource.REDDIT` always means "the browser implementation," full stop.

```python
def _build_reddit(context: CollectorContext) -> RedditBrowserCollector:
    return RedditBrowserCollector(...)

register_collector(DataSource.REDDIT, _build_reddit)
```

**Backward compatibility (audited, confirmed low-risk)**: `storage.py` persists `data_source` as plain
`TEXT` (`run.data_source.value` on write, `DataSource(row["data_source"])` on read — lines 61/145/218/257/307).
Old stored runs with `"reddit_api"`/`"reddit_scraper"` values will always reconstruct correctly regardless
of what the frontend currently offers for *new* runs — **no DB migration needed**. So the smallest safe
move is:
- **Keep** `DataSource.REDDIT_API` and `DataSource.REDDIT_SCRAPER` enum members and their existing
  registered factories, untouched — old runs stay fully readable/replayable.
- **Add** `DataSource.REDDIT` as the new canonical member, routed to `RedditBrowserCollector`.
- **Remove** `"reddit_api"` and `"reddit_scraper"` from `CreateRun.tsx`'s `SOURCE_OPTIONS` (line 9),
  replacing both with a single `"reddit"` entry — new runs only ever see one "Reddit" choice.
  `SOURCE_OPTIONS` goes from `["reddit_api", "reddit_scraper", "amazon", "youtube", "json_upload"]` (5)
  to `["reddit", "amazon", "youtube", "json_upload"]` (4) — a net *simplification* of the frontend, not
  an addition.
- Update `sources.ts`'s `STRUCTURE` map: add a `reddit: {...}` entry (same `groupKey`/`itemKey`/
  `citationPrefix` as today's `reddit_api` entry — `"subreddit"`/`"comment"`/`"r/"`), with a **new**
  `configKey` reflecting Chrome/profile availability rather than PRAW credentials (see below).
- `RedditScraperCollector`'s docstring gets one line added marking it legacy: kept only for backward
  compatibility with pre-existing runs, not a recommended path, currently non-functional (403) in this
  environment. No behavior change, doc-only edit.
- **Concrete frontend touch points found by reading `CreateRun.tsx` directly** (so this is verified, not
  guessed): line 109's `showRedditWarning` check (`dataSource === "reddit_api" && !config.reddit_configured`)
  needs to become `dataSource === "reddit" && !config.reddit_browser_configured` (a **new** config flag
  reflecting Chrome-executable + profile-directory availability, computed the same way `amazon_configured`/
  `youtube_configured` already are — need to locate that config-computation code at implementation time,
  not yet read this session); line 158's `dataSource === "reddit_scraper"` note becomes dead code and
  should be removed (or repurposed as a one-time-setup hint for `dataSource === "reddit"`); line 185's
  `(dataSource === "reddit_api" || dataSource === "reddit_scraper")` subreddit-scoping gate becomes just
  `dataSource === "reddit"`.

This is deliberately **not** a smart/conditional router (e.g. "use official API if creds exist, else
browser") — that's exactly the "complicated fallback framework" the user asked to avoid for v1. `REDDIT`
means the browser implementation, unconditionally, for now. A future enhancement could make the `REDDIT`
factory smarter; not in this plan.

## 2. Chrome lifecycle management

New file: `backend/app/collectors/_reddit_chrome.py` — pure infrastructure, no Reddit-page logic.

**New dependency**: `psutil>=5.9` (add to `pyproject.toml`). **Verified before proposing this**: grepped
the entire backend (`pyproject.toml` + every `app/**/*.py`) for any existing process-management
dependency or helper (`psutil`, `wmi`, `pywin32`, `win32api`/`win32process`) — none exists anywhere in
this codebase today, so there's nothing in-house to reuse. Given that, `psutil` is justified: reliable,
cross-platform process introspection (`cmdline()`, `create_time()`, `pid`) beats hand-parsing
PowerShell/WMI output in production code, and this is the one place in the whole design where getting
process identification *wrong* has real consequences (killing the wrong Chrome). Safety over dependency
minimalism here, as directed.

Core pieces:

- `ProcessIdentity` (frozen dataclass): `pid`, `create_time`, `cmdline_snapshot`, `cdp_port`. Captured
  once at launch time and re-verified (never trusted stale) before any shutdown action.
- `locate_chrome_executable() -> Path` — checks `REDDIT_CHROME_EXECUTABLE` env override first, then the
  standard Windows install path(s); raises a clear error listing what was checked if none found.
- `find_dedicated_instance(profile_dir, cdp_port) -> ProcessIdentity | None` — scans `chrome.exe`
  processes via `psutil.process_iter(["pid", "cmdline", "create_time"])`, matches only processes whose
  cmdline contains **both** `--remote-debugging-port=<port>` **and** an **exact** `--user-data-dir=<profile_dir>`
  (not a substring match). Zero matches → not running. **More than one match → refuse and raise** (never
  guess among candidates — matches the user's "fail safely if not identified with high confidence" requirement).
- `ensure_running(profile_dir, cdp_port) -> ProcessIdentity` — the main entry point a collector calls
  before doing anything:
  1. `find_dedicated_instance(...)`. If found, probe `GET http://localhost:{port}/json/version` for
     liveness and record its `webSocketDebuggerUrl` (the CDP identity signal) — reuse it, skip
     relaunch entirely. This is the common case for repeated runs and is strictly cheaper/safer than a
     fresh cold start every time.
  2. If not found: launch `chrome.exe --remote-debugging-port=<port> --user-data-dir=<profile_dir>`
     with **no other flags** — deliberately plain, no `--headless`, no automation-marker flags — poll
     `/json/version` for readiness (bounded timeout, e.g. 15s), capture the new `ProcessIdentity`.
  3. Never falls back to launching via `agent-browser open`/`--profile` for Reddit — that path is the
     one proven to trigger challenges.
- `shutdown(identity: ProcessIdentity) -> bool` — **explicit, administrative operation only** (see
  below for why this is never auto-invoked per-run):
  1. Re-verify `identity` against a **fresh** process scan (pid **and** `create_time` must both match —
     defends against PID reuse after the original process died and Windows recycled the PID). If it no
     longer matches, treat as "already stopped," do nothing.
  2. Best-effort `agent-browser --cdp <port> close` — **not trusted as proof of anything**, purely a
     polite first attempt.
  3. Poll independently (via psutil) for process exit, bounded timeout (e.g. 5s).
  4. If still alive: `process.terminate()` on the exact verified PID, poll again.
  5. If still alive after a second bounded timeout: `process.kill()` as last resort, same verified PID only.
  6. Final independent verification: re-scan confirms zero matching processes **and** the CDP port no
     longer responds. Both checks logged explicitly — this is the "don't trust the close message"
     requirement, satisfied by never trusting *any* single signal, always re-verifying via a fresh OS-level scan.
- **Never auto-shutdown after a run.** Leave the dedicated Chrome running as a long-lived background
  process, reused across runs — this is strictly cheaper (skips repeated cold starts) and marginally
  safer (fewer executions of the one risky "kill a Chrome process" code path). Shutdown is exposed as an
  explicit maintenance operation (a small CLI entry point or admin-only route), not something the
  collector calls automatically.

## 3. REVISED — Profile management and state model

**Problem with the original design**: treating "`reddit_collector_state.json` doesn't exist" as proof of
`NOT_INITIALIZED` would misclassify the **already-validated** `reddit_cdp_probe_profile` (real Chrome
state, proven live twice, created before this metadata file concept existed) as uninitialized, and
demand the user redo manual setup for a profile that already works. Our own metadata must be treated as
**observed history**, never as the source of truth for whether Reddit session trust exists.

- Config: `REDDIT_CHROME_PROFILE_DIR`, default `~/.reddit-chrome-profile`, deliberately named
  differently from Amazon's `~/.agent-browser-profiles/amazon` (this is a plain Chrome `--user-data-dir`,
  not an agent-browser-managed profile — a different mechanism). Lives outside the repo by default, same
  as every other collector's profile.
- **Revised `ProfileStatus`** — four states, distinguished by two independent, layered signals (Chrome's
  own on-disk footprint vs. our own metadata file):

  | State | Detected by |
  |---|---|
  | `NOT_INITIALIZED` | Profile directory doesn't exist, or exists but has no `Local State` file at its root — Chrome writes that file on its very first launch against a `--user-data-dir`, *before* any navigation, so its absence is a reliable "Chrome has genuinely never run here" signal (distinct from someone just `mkdir`-ing an empty folder) |
  | `UNKNOWN` | `Local State` **is** present (real Chrome profile data exists) but our own `reddit_collector_state.json` is not — exactly the current `reddit_cdp_probe_profile` case |
  | `HEALTHY` | `reddit_collector_state.json` exists and its last recorded outcome was success |
  | `CHALLENGED` | `reddit_collector_state.json` exists and its last recorded outcome was a challenge |
  | `UNAVAILABLE` | Chrome executable missing, CDP never came up, or process identification failed with high-confidence ambiguity (§2) |

- **`NOT_INITIALIZED` behavior**: unchanged from before — do not attempt automated collection; raise a
  `RuntimeError` with the exact one-time manual command (mirrors Amazon's collector docstring pattern).
- **`UNKNOWN` behavior (the fix)**: allowed **exactly one** normal collection attempt — the collector
  just proceeds with its first real `search()` call as usual; there is no separate "probe" request wasted
  before the real one. If that attempt completes without a challenge: **adopt** the profile — write
  `reddit_collector_state.json` for the first time (`initialized_at` = now, `last_success_at` = now),
  transition to `HEALTHY`, and return the real results from that same call. If it hits a challenge:
  write the state file (`initialized_at` = now, `last_challenge_at` = now), transition to `CHALLENGED`,
  stop per the state machine in §3a below. **This adoption path is strictly additive** — it only ever
  *creates* the new metadata file alongside the profile; it never deletes, resets, or relaunches Chrome
  with a different profile. `reddit_cdp_probe_profile` specifically is adopted this way, with zero
  destructive action, the first time it's pointed at by `REDDIT_CHROME_PROFILE_DIR`.
- **`HEALTHY`/`CHALLENGED` behavior**: see the explicit state machine below (§3a) — this is where the
  original plan was ambiguous and needed the retry semantics spelled out.
- `available()` (the `Collector` protocol method) mirrors Amazon's semantics: True if the Chrome
  executable can be located **and** the profile dir exists or can be created — not "currently proven
  healthy," same as Amazon's `available()` checking profile existence rather than live login state.

## 3a. NEW — Explicit challenge/retry state machine

Two independent scopes, as the user specified, composing cleanly:

**Within one run (collector-instance-local, in-memory only)** — confirmed via `run_manager.py` (line 59)
that exactly **one** collector instance is built per run (`build_collector()` called once, passed into
`run_react_loop`, which then calls `collector.search()` once per ReAct iteration on that *same*
instance) — so a plain instance attribute is sufficient, no new plumbing needed anywhere else:

```
self._reddit_disabled_for_run: bool = False   # set the instant a challenge is detected

def search(self, query, subreddit="", limit=25):
    if self._reddit_disabled_for_run:
        self.last_search_stats = {..., "challenge_detected": True, "chrome_reused": True}
        return []          # zero Chrome/CDP interaction at all -- cheapest possible no-op
    ... normal collection ...
    if challenge_detected:
        persist_challenged_state()          # write CHALLENGED to reddit_collector_state.json
        self._reddit_disabled_for_run = True
        self.last_search_stats = {..., "challenge_detected": True}
        return partial_results_collected_so_far   # NORMAL RETURN, never raised -- see §8 fix
```

This guarantees a challenge on iteration 1 of a 10-iteration run means iterations 2–10 never touch
Reddit again — satisfied exactly as asked, with no react_agent.py changes (its existing try/except and
empty-list handling already covers a collector returning `[]`, and a normal-return-with-partial-items
path needs no react_agent.py change either).

**Consistency fix caught during plan review**: an earlier draft of §8 said a challenge should *also*
raise an exception. That directly conflicts with "return partial results" above — `react_agent.py`'s
existing except-branch unconditionally replaces whatever the call returned with `items = []`
(`react_agent.py:115-117`), so if a challenge raised, every already-collected partial item would be
silently discarded, exactly backwards from the intent. **Resolved**: a challenge is a normal return, not
an exception — see the corrected §8 below. Only genuine infrastructure failures (Chrome won't launch,
CDP won't attach, process identification is ambiguous) are true exceptions, because those legitimately
have zero partial data worth preserving.

**Across independent runs (on-disk profile state, read once per new collector instance)**:

```
new run starts -> read persisted ProfileStatus
  NOT_INITIALIZED -> hard stop, RuntimeError, no attempt
  UNKNOWN / HEALTHY / CHALLENGED -> exactly one normal attempt allowed this run
      attempt succeeds -> write HEALTHY, last_success_at=now; run continues normally
      attempt challenged -> write CHALLENGED, last_challenge_at=now, consecutive_challenge_count += 1;
                             self._reddit_disabled_for_run = True (same in-run guard as above)
```

A profile is never permanently bricked by one challenge — every new run gets a fresh chance. It's also
never hammered — the in-run guard means at most one Reddit challenge can ever be *caused* per run,
regardless of how many ReAct iterations follow. `consecutive_challenge_count` is tracked for future
visibility (e.g. a prominent "this profile has failed N runs in a row, consider re-initializing" log
line past some small threshold) but is explicitly **not** a v1 requirement — noted as a future nicety
only, not built now, to avoid over-engineering.

## 4. Search/discovery

- Prefer `data-faceplate-tracking-context` structured JSON (title, subreddit, author, post id, nsfw)
  over text parsing wherever Reddit exposes it — validated directly in both POCs. Vote/comment counts
  are **not** present in that JSON and genuinely require light regex text parsing on the card's
  `innerText` — call this out explicitly as an accepted, isolated fragile point (two fields only), not
  something avoidable.
- Promoted-post exclusion: scoped to the `search-post-unit` card testid plus a text-based
  `promoted`/`ad ·` flag. **Caveat**: never validated against a real promoted result in either POC (both
  queries happened to return zero ads) — flagged as a gap to cover with a synthetic fixture in tests (§14),
  not a proven-solid exclusion.
- **REVISED v1 ranking — query relevance and discussion depth kept as two separate, explicit signals**,
  not conflated into a single comment-count-only sort. Rationale (user's example): for query "MacBook
  battery life," a 500-comment "Show us your MacBook setup!" thread is off-topic despite huge engagement,
  while an 80-comment "M4 MacBook Pro battery life after six months" thread is exactly on-topic despite
  fewer comments. Comment count measures discussion *depth*, not query *relevance* — conflating them
  would have silently favored the wrong post in exactly this kind of case. Still fully deterministic, no
  LLM call, no new external dependency:
  1. Exclude promoted and NSFW (configurable) first — structural filtering, unchanged from before.
  2. **Query-relevance score**: lexical overlap between the query's tokens and the result's title (and
     snippet, when Reddit provides one — neither POC's result set actually had a populated snippet, so
     this degrades to title-only in practice; noted as a real limitation, not hidden). Reuses
     `app/pipeline/text.py`'s existing `simple_similarity()` (the Jaccard-based function Phase 1.6's
     within-review merge logic already relies on) rather than writing a new overlap function — same
     "reuse, don't duplicate" principle the rest of this codebase already follows. Optional small bonus
     if the query/product-category string appears in the subreddit name (e.g. "keyboard" landing in
     r/MechanicalKeyboards).
  3. Apply `REDDIT_BROWSER_MIN_COMMENT_COUNT` as a floor filter (unchanged).
  4. **Sort by relevance score descending first, comment_count descending as the tiebreaker** — a
     two-key sort, not a single blended weighted-sum. Deliberately simpler than a weighted formula: no
     arbitrary weight constants to tune or overfit to the two POC topics, and the ordering stays easy to
     explain ("most on-topic first; among similarly on-topic results, most-discussed first").
  5. Take top `REDDIT_BROWSER_MAX_POSTS_PER_QUERY`.
  This still deliberately does **not** try to replicate the "showcase post" judgment call made manually
  in the POC (the 468-vote "this is my first one" post) — that requires understanding post *intent*, not
  just keyword overlap, and stays a documented v1 limitation.
  6. The whole thing stays behind one pure function, `rank_and_select(query, results, max_posts) -> list[SearchResult]`,
     taking already-parsed data with no I/O — still the seam where a future "Search Planner 2.0"
     (an LLM-based relevance pass over search results, mirroring `screen_item`'s existing pattern) could
     later replace the relevance-scoring step specifically, without touching the discussion-depth
     tiebreaker, navigation, or extraction code.

## 5. Post selection

- `max_posts_per_query`: `REDDIT_BROWSER_MAX_POSTS_PER_QUERY`, default **3** (matches Amazon's
  `_MAX_PRODUCTS_PER_QUERY` convention and both POC runs).
- `min_comment_count`: `REDDIT_BROWSER_MIN_COMMENT_COUNT`, default **5** — skips near-dead threads.
- Comment count is the discussion-depth **tiebreaker**, not the primary signal — query relevance sorts
  first (§4, revised).
- URL dedup: the collector dedups **within one `search()` call's own results** (defends against Reddit
  ever rendering true DOM duplicates, same as the existing extraction JS already does with a `seen href`
  set). It does **not** dedup against earlier iterations — `react_agent.py`'s existing `seen_urls` set
  already owns that job across the whole run; duplicating it in the collector would be redundant logic
  in two places for no benefit.

## 6. Comment extraction → `CollectedItem`

No schema change — confirmed field-by-field against real DOM attributes captured in the POCs:

| `CollectedItem` field | Source |
|---|---|
| `source_url` | `shreddit-comment.permalink` attribute (full path) for comments; post URL for posts |
| `subreddit` | tracking-context JSON `subreddit.name`, or `shreddit-post`'s `subreddit-prefixed-name` |
| `item_type` | `"post"` / `"comment"`, set by the extraction pass |
| `post_id` | `shreddit-comment.postid` (e.g. `t3_...`) |
| `comment_id` | `shreddit-comment.thingid` (e.g. `t1_...`) |
| `title` | tracking-context JSON `post.title`, or `shreddit-post`'s `post-title` |
| `body` | `shreddit-comment.querySelector('[slot="comment"]').innerText` — clean, no header/footer noise (confirmed better than raw `innerText`, which was only used ad hoc during the POC) |
| `score` | `shreddit-comment`/`shreddit-post`'s `score` attribute |
| `comment_count` | `shreddit-post`'s `comment-count` attribute (posts only) |
| `created_at` | `shreddit-comment`'s `created` attribute — **already full ISO8601**, cleaner than the Unix-timestamp math the scraper/PRAW collectors do |
| `search_query` | passed through by the collector, same as every other collector |

## 7. Comment loading strategy (deliberately conservative v1)

1. Extract whatever's initially rendered — no interaction at all. Both POCs got 20–25 comments/post this
   way **even on threads whose real total was much higher** (e.g. 268 total comments on one post, only
   25 initially rendered) — recorded explicitly here as a known v1 yield ceiling, not hidden. If
   downstream Claim volume ever proves this insufficient, scrolling-based comment discovery is the
   natural **v1.1** improvement — not attempted in this plan. v1's goal is stable, useful VOC sampling,
   not exhaustive scraping.
2. **At most one** attempt to find a genuinely-visible "View more comments" control, using the exact
   visibility-verification technique proven in the POCs (CSS `display`/`visibility`/`opacity`/geometry
   checks, explicitly rejecting the invisible-duplicate-element trap seen twice). If zero or **more than
   one** unambiguous visible match: skip, do not guess — exactly what was actually done in both POCs.
3. **No scrolling to hunt for a control** in v1 — later POCs deliberately didn't do this either; flagged
   as a future enhancement, not a v1 requirement.
4. `max_comments_per_post`: `REDDIT_BROWSER_MAX_COMMENTS_PER_POST`, default **50** — a safety valve, not
   a target (typical observed yield is 20–30).
5. No nested-reply expansion, ever, in v1.
6. Dedup by `thingid` within a post before returning — 0% duplicate rate was observed in both POCs, but
   the safety net stays (and gets a fixture-forced test case, since the POC never actually exercised the
   duplicate-removal code path for real).
7. Kill switch: `REDDIT_BROWSER_ALLOW_VIEW_MORE_CLICK` (default `true`) — set `false` to disable all
   interaction beyond reading, matching the `ENABLE_CLAIM_EXTRACTION`-style toggles already in this codebase.

## 8. REVISED — Challenge/block detection (return, don't raise)

Reuse the exact detector validated across every experiment this session:
- URL contains `js_challenge`
- body text (lowercased) contains `"blocked by network security"` / `"access denied"`
- body text contains `"verify you are human"` / `"captcha"`
- **defense in depth**: expected DOM absent (`shreddit-post` missing on what should be a post page, zero
  result cards on a search page) even if none of the above strings match — durable against Reddit
  changing block-page wording.

**On detection, exact behavior (per explicit user correction — a challenge is a partial/empty result, not an exception)**:
1. Stop all further navigation immediately — no more search/post/comment calls within this `search()` call.
2. Persist `CHALLENGED` to the profile's state file (§3).
3. Set the in-run guard `self._reddit_disabled_for_run = True` (§3a).
4. **Return normally** — `list[CollectedItem]` containing whatever was successfully collected before the
   challenge was hit (may be empty, never discarded).
5. **Do not raise an exception for the challenge itself.** Raising would let `react_agent.py`'s existing
   except-branch (`react_agent.py:115-117`) silently overwrite the return value with `items = []`,
   destroying exactly the partial data step 4 is supposed to preserve — this was the inconsistency
   flagged in review between the original §3a and §8 and is the reason for this revision.
6. Set `challenge_detected: True` in `last_search_stats` (§11) so the existing `ACTION_SEARCH` trace
   event can show what happened, since no exception means react_agent's `"Search failed: {exc}"` trace
   branch never fires for this case — the challenge is visible via the stats payload instead.
7. Every subsequent `search()` call in the same run returns `[]` immediately per the in-run guard, with
   zero Chrome/CDP interaction (§3a).

**What still legitimately raises**: genuine infrastructure failures only — Chrome can't launch, CDP can't
attach, process identification is ambiguous (§2/§9). These have no partial data to preserve, so letting
`react_agent.py`'s existing try/except turn them into a traced `"Search failed: {exc}"` with `items=[]`
is correct and matches the original design — the fix here is scoped specifically to the challenge case,
not to infrastructure failures.

No retry, no bypass attempt, ever, in either case.

## 9. Failure and fallback behavior

| Failure | Behavior |
|---|---|
| Chrome can't launch | `RuntimeError` from `available()`/first call; run continues via react_agent's existing try/except, 0 items that iteration |
| CDP port unavailable | Same |
| agent-browser can't attach | Same |
| Profile challenged | Normal return with partial (possibly empty) results, `challenge_detected=True` in stats, no exception (§8, revised) |
| Search returns no results | Empty list, **not** an error — matches Amazon's existing "no products found" behavior |
| One post navigation fails | Log/skip that post, continue with the other selected posts (partial success within one call, not all-or-nothing) |
| DOM selectors change (Reddit redesign) | Extraction JS uses defensive optional-chaining (`?.`) throughout, same style already used in Amazon/YouTube extractors — degrades to missing fields, not a hard crash. Accepted as an ongoing maintenance risk, same as the existing collectors already carry. |
| Comments fail to load | Proceed with whatever's rendered; 0 comments for a post is a valid outcome, not an error |

## 10. Integration with current pipeline

```
plan_next_query()              [UNCHANGED — existing Search Planner]
   -> collector.search(query, subreddit, limit=25)   [UNCHANGED call site, react_agent.py:108]
        -> RedditBrowserCollector (NEW):
             ensure Chrome running -> Reddit search nav -> extract+rank+select posts
             -> per-post nav + comment extraction -> list[CollectedItem]
   -> back in react_agent.py's existing loop: screen_item() -> extract_claims()   [UNCHANGED]
```
Zero changes to Phase 1 or Phase 2 pipeline code. The entire integration is additive at the collector layer.

## 11. REVISED — Observability, simplified (no new abstraction)

Dropped the proposed `RedditBrowserSearchStats` dataclass — on reflection it's unnecessary ceremony for
nine plain fields with no behavior attached to them. Revised to: `collector.last_search_stats` is just a
plain `dict[str, Any]`, set after each `search()` call, exposed via the same **duck-typed, opt-in**
pattern already established in this codebase (`run_manager.py` already calls `collector.close()` via
`getattr(...)`, not through the `Collector` Protocol — same precedent, same mechanism, no new pattern
introduced). Fields, exactly the nine the user listed, nothing extra: `queries_attempted`,
`search_results_discovered`, `posts_selected`, `posts_opened`, `comments_extracted`, `comments_unique`,
`challenge_detected` (bool), `chrome_reused` (bool, vs. cold-started), `duration_ms`.

Same one small, additive, optional touch to `react_agent.py` as before: after the existing
`collector.search()` trace call (line ~109), read `getattr(collector, "last_search_stats", None)` and
merge into the existing `ACTION_SEARCH` trace payload if present — a no-op for every other collector,
zero behavior change for Amazon/YouTube/JSON-upload/PRAW. Still flagged for explicit approval since it's
the one line outside new files; happy to skip and keep stats collector-internal-only (e.g. just logged)
if preferred.

## 12. Configuration (new `.env.example` entries, Amazon-comment-block style)

```
# Reddit browser collector: a plain, independently-launched Chrome instance (NOT agent-browser's own
# managed browser) that agent-browser attaches to via CDP. Requires one-time manual initialization —
# see collectors/reddit_browser.py docstring for the exact command. No Reddit login required.
REDDIT_CHROME_EXECUTABLE=
REDDIT_CHROME_PROFILE_DIR=
REDDIT_CHROME_CDP_PORT=9222
REDDIT_BROWSER_MAX_POSTS_PER_QUERY=3
REDDIT_BROWSER_MAX_COMMENTS_PER_POST=50
REDDIT_BROWSER_MIN_COMMENT_COUNT=5
REDDIT_BROWSER_ALLOW_VIEW_MORE_CLICK=true
```
Follows the exact existing convention (`os.environ.get()` + `load_dotenv()`) — no new settings-module
infrastructure, since none exists in this codebase today.

## 13. Security

- Profile dir defaults outside the repo (structurally safe); a defensive `.reddit-chrome-profile/`
  pattern added to `.gitignore` anyway in case someone points it locally.
- `.env` already gitignored — new vars automatically covered.
- Normal Chrome never touched — enforced by §2's exact cmdline+profile-path match, refuse-if-ambiguous.
- CDP stays local — Chrome's `--remote-debugging-port` without `--remote-debugging-address` is
  loopback-only by default; the launch command never passes `--remote-debugging-address`.
- Shutdown safety — covered exhaustively in §2 (re-verify identity from a fresh scan immediately before
  any terminate/kill call, never act on a stale in-memory PID).

## 14. Test plan

Unit tests (no real Chrome), following the existing `FakeSession`-injection style from `test_scraper_collector.py`:

- `test_reddit_chrome_lifecycle.py`: process-identity matching against **fake** process listings —
  exact match, no match, ambiguous multiple matches (must refuse), stale-PID-reuse defense (PID matches
  but `create_time` doesn't → refuse).
- `test_reddit_browser_collector.py`: search-result parsing from a saved real fixture (captured
  `data-faceplate-tracking-context` + card HTML from this session's POC); promoted-post exclusion via a
  **synthetic** fixture (real POC data never had one); **relevance-vs-discussion-depth ranking** given a
  synthetic fixture built specifically to reproduce the "MacBook battery life" ordering problem (a
  high-comment-count off-topic result must rank below a low-comment-count on-topic one); comment DOM
  parsing from a fixture; dedup-by-`thingid` with a fixture containing a **deliberate** duplicate (the
  POC never actually exercised this path for real); challenge-detection classifier against canned
  URL/body samples; `CollectedItem` field-mapping; graceful degradation when a field is missing from a
  fixture (simulates a DOM/redesign change); **profile-state model** — `NOT_INITIALIZED` vs. `UNKNOWN`
  classification given a fake filesystem with/without `Local State` and with/without
  `reddit_collector_state.json` (proving an existing profile like `reddit_cdp_probe_profile`-shaped
  fixture data is classified `UNKNOWN`, not `NOT_INITIALIZED`); **challenge/retry state machine** — the
  in-run `_reddit_disabled_for_run` guard actually suppresses a second `search()` call after a first
  challenge within one collector instance, and a fresh collector instance (simulating a new run) is
  allowed exactly one attempt against a persisted `CHALLENGED` state; **challenge-return contract** —
  given a fixture where some posts succeed before a mid-run challenge is hit, `search()` must return
  those already-collected `CollectedItem`s normally (not raise), with `last_search_stats["challenge_detected"] is True`
  — directly guards against the return-vs-raise inconsistency caught during plan review.
- `test_collectors_registry.py`: extend to confirm `DataSource.REDDIT` resolves to the `RedditBrowserCollector`
  factory, and that `DataSource.REDDIT_API`/`REDDIT_SCRAPER` still resolve to their original, untouched factories.

Separate, **not** pytest-collected: `backend/scripts/reddit_browser_smoke_test.py` — requires a real,
already-initialized profile; prints a human-readable pass/fail report; naturally excluded from `pytest`
since `pyproject.toml`'s `testpaths = ["tests"]` never looks in `scripts/`. Documented usage in the script's own header.

## 15. Migration / compatibility (REVISED to match §1)

- `RedditScraperCollector` / `RedditCollector`: code untouched (factories stay registered under
  `REDDIT_API`/`REDDIT_SCRAPER` exactly as today, for backward-compatible replay of old runs);
  `RedditScraperCollector` gets one doc-only line marking it legacy/non-working in this environment.
- Purely additive at the enum level: one new `DataSource.REDDIT` value, one new registration call — no
  existing enum values or stored run data change meaning.
- **Confirmed this session** (not deferred): `storage.py` persists `data_source` as plain `TEXT`
  (`run.data_source.value` on write — line 257; `DataSource(row["data_source"])` on read — line 307),
  and the column itself already exists (`ALTER TABLE runs ADD COLUMN data_source TEXT NOT NULL DEFAULT
  'reddit_api'` — line 145, from when it was first introduced). Adding a new valid string value needs
  **no migration**, unlike `pipeline_version`, which needed one because that was a *new column* — this
  is a new value in an existing column, the same shape as how AMAZON/YOUTUBE were added previously.
- Frontend: **not** a 6th option added to `CreateRun.tsx`'s existing 5 — the two Reddit entries
  (`reddit_api`, `reddit_scraper`) are **replaced** by one (`reddit`), net result 4 total options, a
  simplification. Full detail in §1.

## Files to add/change

**New:**
- `backend/app/collectors/_reddit_chrome.py`
- `backend/app/collectors/reddit_browser.py`
- `backend/tests/test_reddit_chrome_lifecycle.py`
- `backend/tests/test_reddit_browser_collector.py`
- `backend/scripts/reddit_browser_smoke_test.py`

**Changed:**
- `backend/app/models.py` — add `REDDIT = "reddit"` to `DataSource` (new canonical member); add a
  comment above `REDDIT_API`/`REDDIT_SCRAPER` noting they're retained only for backward-compatible reads
  of pre-existing runs and are no longer offered for new runs
- `backend/pyproject.toml` — add `psutil>=5.9` (verified no in-house alternative exists, §2)
- `backend/.env.example` — new config block (§12)
- `.gitignore` — defensive profile-dir pattern
- `backend/tests/test_collectors_registry.py` — extend to cover `DataSource.REDDIT`
- `backend/app/collectors/scraper.py` — doc-only edit marking `RedditScraperCollector` legacy/non-working
  in this environment (§1), no behavior change
- `backend/app/react_agent.py` — one small, optional, additive trace-payload enrichment (§11) —
  **flagging for explicit approval since it's the one line outside new files**
- `frontend/src/pages/CreateRun.tsx` — `SOURCE_OPTIONS` (line 9) simplified from 5 entries to 4
  (`reddit` replaces `reddit_api`+`reddit_scraper`); update the three concrete touch points found by
  reading the file directly: line 109 (`showRedditWarning`), line 158 (dead `reddit_scraper` note), line
  185 (subreddit-scoping gate) — all detailed in §1
- `frontend/src/lib/sources.ts` — add a `reddit` entry to `STRUCTURE` (§1); exact config-flag wiring
  (`reddit_browser_configured` or similar) needs locating the existing `amazon_configured`/
  `youtube_configured` computation at implementation time — not yet read this session

## Acceptance criteria (REVISED)

- The frontend exposes exactly **one** user-facing Reddit source for new runs (`SOURCE_OPTIONS` no
  longer contains `reddit_api`/`reddit_scraper`).
- New Reddit runs use `DataSource.REDDIT`, routed to the validated `RedditBrowserCollector`
  (Chrome + dedicated persistent profile + CDP + agent-browser attach).
- Existing stored runs using legacy `reddit_api`/`reddit_scraper` `DataSource` values remain fully
  readable/replayable — confirmed zero DB migration needed (`data_source` is plain `TEXT`, old string
  values always reconstruct via `DataSource(row["data_source"])`).
- A pre-existing, already-usable Chrome profile with no `reddit_collector_state.json` (e.g.
  `reddit_cdp_probe_profile`) is adopted automatically through exactly one health attempt — never
  requires redoing manual initialization, never destructively touched during adoption.
- A challenge stops all further Reddit attempts for the remainder of the current run (collector-local
  in-memory guard, verified sufficient given `run_manager.py` builds one collector instance per run), and
  returns already-collected partial results **normally** (never via a discarded exception — see §8 fix).
- A future independent run may attempt a `CHALLENGED` profile again exactly once; success returns it to
  `HEALTHY`, a repeat challenge keeps it `CHALLENGED` without permanently blocking future runs.
- Query relevance (title/snippet/subreddit lexical overlap, reusing `text.py`'s `simple_similarity`) and
  discussion-depth (comment count) are conceptually and implementationally separate signals, combined via
  a two-key sort, not a single blended heuristic.
- Phase 1 (Claim extraction) and Phase 2 (Screening) remain completely untouched — the collector collects
  `CollectedItem`s only, never screens or generates Claims.
- No DB migration performed, since the audit proved none is required.
- Chrome lifecycle never identifies or terminates a process without an exact, freshly-re-verified
  cmdline + profile-path + PID + create-time match; ambiguity always fails safe (raises, never guesses);
  `agent-browser`'s CDP `close` output is never trusted as proof of termination.
- Full unit suite passes without a real Chrome instance; the separate smoke test is documented and
  excluded from normal `pytest` runs.
- No new production dependency beyond `psutil`; no Playwright.
- Long-term profile durability across many days/runs remains an explicit monitoring item, not a blocker
  for this integration.

## Known limitations (carried forward honestly, not hidden)

- Long-term profile durability across many days/runs is still an open monitoring item — two successful
  restarts, close together in time, on one network, is real signal but not proof of durability.
- Promoted-post exclusion logic has never been exercised against a real promoted result.
- v1 relevance ranking (lexical title/snippet overlap + comment-count tiebreak) doesn't catch
  showcase-style posts the way the manual POC judgment did, and snippet text was empty in both POCs'
  actual result sets — ranking degrades to title-only overlap in practice. Documented as accepted, not solved.
- No scrolling-based comment-expansion in v1; typical yield stays in the 20–30 comments/post range even
  when the real thread total is much higher (e.g. 25 of 268 observed in one POC post).
- Trust's dependence on network/IP hasn't been tested (all experiments happened on one network since the
  mobile-hotspot switch).

## New risks introduced by this revision (vs. the original plan)

- The `UNKNOWN`-state one-time "adopt" attempt means the first collector use after this ships (or after
  pointing at any pre-existing profile) makes one live Reddit request as a side effect of state
  detection — not an *extra* call (it reuses the first real search as the trust check), but worth naming
  explicitly since it wasn't framed as a live-network side effect before.
- Simplifying observability to a plain `dict` (from a proposed dataclass) trades away static type-checking
  on the stats fields — an accepted, minor tradeoff for the requested simplicity.
- Query-relevance scoring via lexical overlap can behave weakly on very short or very generic queries
  (little text to overlap against) — a known edge case, not a blocker, and isolated behind the same
  single pure function that a future smarter ranker would replace.
- Hiding `reddit_api`/`reddit_scraper` from the new-run UI while keeping them registered means three
  frontend touch points (§1) must all be updated consistently, or a stale reference to the old values
  could linger in the UI — now precisely scoped since the actual file/line references were read directly
  this session, not guessed.

## Future improvements (not in this plan)

- "Search Planner 2.0": LLM-based relevance judgment over search results before spending navigation
  cost, replacing the deterministic `rank_and_select()` seam described in §4.
- Scrolling-based comment discovery, if 20–30 comments/post proves insufficient for downstream Claim volume.
- Periodic automated durability re-checks (e.g. a scheduled smoke-test run) to build real evidence on
  the open long-term-durability question rather than leaving it purely as a caveat.
