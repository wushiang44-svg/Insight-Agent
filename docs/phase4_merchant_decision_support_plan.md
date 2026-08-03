# Phase 4 — Merchant Decision Support (Approved Scope, Post-Validation)

**Status: implemented and validated.** This document supersedes the original 4-option architecture
proposal (A: Reliability & Demand Validation Layer, B: Semantic Taxonomy Unification, C: Multi-Tenant
Production Hardening, D: Merchant Decision Support) and Option D's original 6-stage plan, both of which
existed only in chat prior to this document. Option D's product framing is unchanged: this is an
evidence-driven VOC analysis platform for merchants/PMs, not an AI business consultant — recommendations
must stay short, evidence-backed, conservative, and traceable to a specific collected-VOC aggregate group,
never inventing ROI/cost/pricing/market strategy the data can't support.

**Final validation (post-implementation):** P0 and P1 were re-run against real stored data for
`run_89b02b9b1e3e` and `run_66b65bc32dc8` (two zero-evidence runs — confirmed the LLM-fabrication bug is
fixed, `_summarize_llm()` is never called, the deterministic empty-state message is returned instead) and
against `run_55025c50e81b` (Claims path) and `run_005ede908bb5` (legacy path) with real DeepSeek API calls
(confirmed the rewritten prompt's guardrails eliminated the previously-observed unsupported
pricing/ROI/technical-specification inventions, e.g. "5200 mAh," "5-10 Hz," "reassess pricing strategy,"
while every recommendation still named an exact supplied aggregate label). Full backend suite: 307/307
passed. Key Takeaways and structured `{text, grounded_in}` traceability remain deferred, not implemented.

Before implementation began, the 6-stage plan's own self-critique ("possible the current
`recommended_actions` already lands within the evidence-backed/conservative boundary in practice — never
empirically checked") was acted on: 9 real reports were pulled from
`backend/data/reddit_insight_agent.sqlite3` across 8 product categories, cross-checked against their
underlying `evidence`/`claims` rows and against `_summarize_llm()`/`_summarize_fallback()`
(`react_agent.py`). That review is why the scope below is much smaller than 6 stages, and shaped
differently — the most severe issue found (fabrication with zero input evidence) is not something the
original 6-stage plan targeted at all.

## Empirical findings (rationale for everything below)

- **Unconditional fabrication when there is no evidence.** `_summarize_llm()` is called with whatever
  `top_pain_points`/`feature_requests`/`praised_aspects` the run produced, with no check for whether those
  lists are empty. Two sampled reports — `run_89b02b9b1e3e` (robot vacuum cleaners, `report_source =
  legacy_evidence`, `fallback_reason = no_claims`) and `run_66b65bc32dc8` (airpods, `legacy_evidence`) —
  both have **zero rows** in `evidence` and `claims` for that `run_id`, yet the saved `Report` contains a
  confident, detailed, plausible narrative (specific pain points, specific fixes) that cannot have come
  from anything collected. Both runs show `status = completed` and render identically to a real report —
  a merchant has no way to tell the difference. By contrast, `run_65fbe971fbba` (MacBook, also zero
  evidence) correctly produced `_summarize_fallback()`'s honest "No Reddit Evidence Available... no
  actionable recommendations can be made" message — proving the honest-decline behavior already exists in
  the codebase, it's just unreachable on the LLM path.
- **Invented pricing/business-strategy advice.** The one sampled report actually built on Phase 3's Claims
  path with real, substantial evidence (`run_55025c50e81b`, robot vacuum cleaners, 155 evidence / 285
  claims) recommended "reassess pricing strategy... consider mid-tier options that bundle self-emptying
  and mopping without the high cost" — strategy advice not supported by the data, which contains exactly
  one complaint that a $1500 unit felt expensive.
- **Invented false-precision technical specs.** The same report also invents specific numbers nowhere in
  the evidence: "increase battery capacity to at least 5200 mAh," "5-10 Hz LiDAR," "quick top-up to 80% in
  30 minutes." The pattern repeats in `run_89b02b9b1e3e`'s fabricated report too ("under 55 dB," "at least
  2.5 hours") — present in both a well-evidenced and a fully-hallucinated report, which is why it's treated
  as a systemic prompt gap rather than an isolated bad output.
- **Traceability is topic-level only.** Across every LLM-narrative report sampled (`run_55025c50e81b`,
  `run_005ede908bb5` dog chew toy, plus the two fabricated ones), recommendations map informally to real
  aggregate labels but never state which aggregate group backs a given claim.
- **The deterministic fallback template is fully grounded but adds no value.** 4/9 sampled reports (yoga
  mat, mechanical keyboard, electric kettle, bluetooth speaker) used `_summarize_fallback()`'s literal
  template (`"X" has a high volume of feedback (N item(s)); recommend investigating and improving it
  first.`) — 100% traceable, zero actionable content. Real and worth a future ticket, but orthogonal to
  this phase's "don't invent things" mission and explicitly out of scope here (see Deferred Ideas).

## Scope

### P0 — System integrity fix: no-evidence grounding guard

**Problem it solves:** a merchant currently cannot tell a real report from a fabricated one.

**Evidence:** `run_89b02b9b1e3e` and `run_66b65bc32dc8` (0 evidence rows, `status = completed`, fully
invented content) versus `run_65fbe971fbba` (0 evidence rows, correctly honest) — see above.

**Change:** in `summarize()` (`react_agent.py`), before the `llm.available()` branch that calls
`_summarize_llm()`, check whether `report_inputs.top_pain_points`, `feature_requests`, and
`praised_aspects` are all empty. If so, route directly to `_summarize_fallback()`'s existing empty-state
branch instead of calling the LLM at all — no new prompt, no new honest-decline copy to write, since
`_summarize_fallback()` already produces the right message when its inputs are empty. This is a
control-flow gate, not new generation logic.

**Why it must ship first:** it is cheaper than P1 (no prompt-engineering judgment calls, a single
boolean check) and fixes the single most severe trust problem found — worse than anything the original
6-stage plan anticipated, since it's not a matter of degree (overly generous narrative) but of kind (a
complete invention presented as a real result).

### P1 — Prompt grounding improvements

**Problem it solves:** on runs that *do* have real evidence, the narrative still invents unsupported
business strategy and fabricates precise-sounding numbers, and never states which aggregate a
recommendation is grounded in.

**Evidence:** `run_55025c50e81b` (pricing-strategy invention, spec fabrication) and `run_005ede908bb5`
(dog chew toy — comparatively well-behaved, useful as a "what good output looks like" reference while
writing the new prompt).

**Change (prompt-only, no schema/DB change):** rewrite `_summarize_llm()`'s system prompt to:
1. Forbid inventing ROI, pricing, implementation cost, or market/business strategy not directly present
   in the input evidence.
2. Forbid inventing specific quantitative technical specs (measurements, percentages, durations,
   capacities, frequencies) not present in the input `pain_points`/`feature_requests`/`praised_aspects`.
3. Require each recommendation to explicitly name the aggregate label (e.g. "floor damage," "mopping
   performance") it addresses, drawn only from labels actually passed into the prompt.

**Why still justified:** this is the original Stage 1 idea from the 6-stage plan, confirmed real by
validation — widened to cover the false-precision-spec pattern the original proposal didn't anticipate,
narrowed to prompt-only (no structured schema — see Deferred Ideas for why).

### Validation

- Re-run the updated `_summarize_llm()`/`summarize()` against a small, deliberately mixed sample:
  - A zero-evidence run (confirm P0 routes to the honest empty-state message, no LLM call).
  - A well-evidenced run in the same category as `run_55025c50e81b` (confirm no invented pricing/specs,
    confirm each recommendation names its backing label).
  - A category that previously hit the fallback template (confirm that path is unaffected by this change).
- Read the new output end-to-end using the same 6 questions from the empirical review, as a merchant
  would, to confirm both fixes actually hold rather than just looking plausible.
- No formal multi-day architecture/release review cycle — this is a scoped, prompt/control-flow-only
  change with no new schema, table, or pipeline stage, so Phase 3's heavyweight review process doesn't fit
  it. A single validation pass against real data is proportional to the change.

## Deferred Ideas (postponed, not abandoned)

- **Key Takeaways** — new `reports.key_takeaways` column, LLM + deterministic-fallback generation, and a
  frontend panel above `Report.tsx`'s KPI cards. Deferred because summarizing a narrative that was, until
  P0/P1 ship, sometimes fully fabricated would have amplified the problem rather than fixed it. Revisit
  once P0/P1 are shipped and validated.
- **Richer traceability** — the original Stage 3's structured `{text, grounded_in}` per-recommendation
  objects (a `Report` schema change). Deferred because the empirical review found no case the lightweight
  "name the label in prose" approach (P1, item 3) couldn't handle, and a structured field doesn't itself
  prevent fabrication — a model can invent a `grounded_in` reference just as easily as it invents a
  narrative claim, unless separately validated. Worth reconsidering only if P1's lighter approach proves
  inadequate once shipped.
- **Report UX improvements beyond Key Takeaways** — e.g. explicit priority/severity tiers on
  `recommended_actions`, more prominent "backed by N mentions across M subreddits" framing. Flagged as
  worthwhile by the review's Q4/Q5 answers, but out of scope for this pass.
- **Heavyweight architecture + release review stage** (mirroring Phase 3's process) — dropped for this
  scope. Reserved for phases with genuinely new pipeline/schema surface area, which this one deliberately
  has none of.
- **Fallback-template output quality** (4/9 sampled reports have zero actionable substance despite being
  fully grounded) — real and worth its own future ticket, but explicitly not Phase 4 scope: Phase 4 is
  about not inventing things, not about enriching an already-conservative template.

## Implementation roadmap

1. P0 — add the no-evidence grounding guard in `summarize()` (`react_agent.py`).
2. P1 — rewrite `_summarize_llm()`'s system/user prompt per the three requirements above.
3. Validation — the 3-run mixed sample + 6-question read-through described above.
4. Ship. Everything in Deferred Ideas moves to a separately-scoped future mini-phase, revisited only after
   P0/P1 are live and validated against real runs.
