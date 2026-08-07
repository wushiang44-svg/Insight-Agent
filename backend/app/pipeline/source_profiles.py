from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..models import DataSource

# "always": the LLM may freely propose any group value (Reddit's existing,
# unrestricted subreddit behavior). "never": the concept doesn't exist for
# this source at all -- the planner must not ask for or invent one. "if_supplied":
# the concept exists only when the run itself was given target groups to
# narrow by; otherwise the planner must leave it empty, never invent one.
GroupingMode = Literal["always", "never", "if_supplied"]


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Static, declarative per-DataSource metadata that parameterizes the
    search-planning (`plan_next_query`) and sufficiency-judging
    (`check_sufficiency`) LLM prompts in react_agent.py.

    Deliberately data only -- no adaptive/learned values, no persistence, no
    behavior, no plugin framework. Milestone 4 / D2 scope: source-aware
    prompt text, nothing else. See react_agent.py's `_plan_next_query_llm()`/
    `_check_sufficiency_llm()` for how each field is consumed.
    """

    display_noun: str
    grouping_mode: GroupingMode
    # User-facing name for the grouping unit -- read two ways depending on
    # grouping_mode: when grouping_mode allows requesting a narrowing filter
    # ("always"/"if_supplied"), this names that filter ("subreddit",
    # "category"). When grouping_mode is "never" (no requestable filter
    # exists), this instead names the source's real, already-collected
    # grouping dimension purely for the sufficiency judge's diversity
    # wording ("product" for Amazon, "video" for YouTube) -- "can't request
    # a narrowing filter" and "has no meaningful diversity concept" are NOT
    # the same fact, so this is never left empty just because grouping_mode
    # is "never". See _check_sufficiency_llm()'s diversity_noun.
    grouping_label: str


_REDDIT_PROFILE = SourceProfile(
    display_noun="Reddit discussions",
    grouping_mode="always",
    grouping_label="subreddit",
)

_SOURCE_PROFILES: dict[DataSource, SourceProfile] = {
    DataSource.REDDIT: _REDDIT_PROFILE,
    # Legacy sources retired from new-run selection (see models.py's
    # DataSource docstring) but still resolvable for pre-existing runs --
    # same Reddit-shaped planning behavior as REDDIT.
    DataSource.REDDIT_API: _REDDIT_PROFILE,
    DataSource.REDDIT_SCRAPER: _REDDIT_PROFILE,
    DataSource.AMAZON: SourceProfile(
        display_noun="Amazon product reviews",
        grouping_mode="never",
        # Not a requestable planner filter (AmazonCollector.search() ignores
        # it) -- named here only so the sufficiency judge can say "product
        # diversity" instead of a generic, less actionable "source
        # diversity". Matches AmazonCollector._normalize_review(), which
        # sets CollectedItem.subreddit to the product title.
        grouping_label="product",
    ),
    DataSource.YOUTUBE: SourceProfile(
        display_noun="YouTube videos and comments",
        grouping_mode="never",
        # Same rationale as Amazon above -- YoutubeCollector._normalize_comment()
        # sets CollectedItem.subreddit to the video title.
        grouping_label="video",
    ),
    DataSource.JSON_UPLOAD: SourceProfile(
        display_noun="an uploaded customer-feedback dataset",
        grouping_mode="if_supplied",
        grouping_label="category",
    ),
}


def get_source_profile(data_source: DataSource) -> SourceProfile:
    """Looks up the planning profile for `data_source`, falling back to the
    Reddit-shaped profile for any value not in the table -- mirrors
    frontend/src/lib/sources.ts's useSourceMeta(), which falls back to
    Reddit's shape for an unknown/legacy DataSource for the same reason: a
    future DataSource added to the enum without a table entry here degrades
    to today's behavior instead of crashing the ReAct loop."""
    return _SOURCE_PROFILES.get(data_source, _REDDIT_PROFILE)
