from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from ..llm import load_dotenv
from ..models import CollectedItem, DataSource, utc_now
from ..text import short_quote, simple_similarity
from ._reddit_chrome import (
    CdpSession,
    ChromeLifecycleError,
    ProfileStatus,
    ensure_running,
    locate_chrome_executable,
    profile_status,
    read_profile_state,
    write_profile_state,
)
from .base import CollectorContext
from .registry import register_collector

# Serializes all Reddit-browser collection across concurrent runs. Unlike
# Amazon/YouTube (one agent-browser-managed tab per run_id), this collector
# deliberately shares ONE long-lived, human-warmed Chrome instance across
# every run -- two runs navigating that same tab at once would silently
# corrupt each other's results the same way an un-scoped agent-browser
# session does. Not called out explicitly in the original plan text; added
# during implementation as a necessary correctness fix for the shared-Chrome
# architecture, consistent with the plan's existing "process sequentially,
# not concurrently" principle.
_SEARCH_LOCK = threading.Lock()

_CHALLENGE_URL_MARKER = "js_challenge"
_BLOCK_PHRASES = ("blocked by network security", "access denied")
_CAPTCHA_PHRASES = ("verify you are human", "captcha")

# Stable, public reason codes for WHY classify_page()/is_challenge() decided a
# page is a challenge -- deliberately fixed string literals, not free text, so
# they can be persisted into trace_events and relied on by any downstream
# consumer (diagnostics UI, future analytics/self-improving tooling) without
# re-parsing prose. Renaming one of these is a breaking change for whatever
# reads trace_events, not a cosmetic tweak -- add a new value instead of
# repurposing an old one if the detection logic ever grows a new signal.
CHALLENGE_REASON_URL_MARKER = "js_challenge_url"
CHALLENGE_REASON_BLOCK_PHRASE = "block_phrase"
CHALLENGE_REASON_CAPTCHA_PHRASE = "captcha_phrase"
CHALLENGE_REASON_EMPTY_TITLE_SHORT_BODY = "empty_title_short_body"
# Not returned by classify_page() -- stamped directly by _search_locked()'s
# own short-circuit branch, for the same reason: a downstream reader of a
# later iteration's trace event should see WHY it was skipped (an earlier
# iteration in this same run already hit a real challenge), not just another
# unexplained `challenge_detected: true`.
CHALLENGE_REASON_RUN_DISABLED = "run_disabled_after_earlier_challenge"

_SEARCH_EXTRACT_JS = r"""
JSON.stringify((function() {
  var cards = Array.from(document.querySelectorAll("div[data-testid='search-post-unit']"));
  return cards.map(function(card) {
    var titleLink = card.querySelector("a[data-testid='post-title']");
    var trackerEl = card.querySelector("[data-faceplate-tracking-context]") || card.closest("[data-faceplate-tracking-context]");
    var ctx = null;
    if (trackerEl) {
      try { ctx = JSON.parse(trackerEl.getAttribute('data-faceplate-tracking-context')); } catch (e) { ctx = null; }
    }
    var text = (card.innerText || '').trim();
    var voteMatch = text.match(/([\d,.]+[kKmM]?)\s*votes?/);
    var commentMatch = text.match(/([\d,.]+[kKmM]?)\s*comments?/);
    var lowText = text.toLowerCase();
    var promoted = lowText.indexOf('promoted') !== -1;
    return {
      title: (ctx && ctx.post && ctx.post.title) || (titleLink ? titleLink.getAttribute('aria-label') : null),
      url: titleLink ? titleLink.href.split('?')[0] : null,
      subreddit: ctx && ctx.subreddit ? ctx.subreddit.name : null,
      postId: ctx && ctx.post ? ctx.post.id : null,
      nsfw: !!(ctx && ctx.post && ctx.post.nsfw),
      voteText: voteMatch ? voteMatch[1] : null,
      commentText: commentMatch ? commentMatch[1] : null,
      promoted: promoted
    };
  });
})())
"""

_POST_META_JS = r"""
JSON.stringify((function() {
  var post = document.querySelector('shreddit-post');
  if (!post) return null;
  var bodyEl = document.querySelector("shreddit-post [slot='text-body'], shreddit-post");
  return {
    title: post.getAttribute('post-title'),
    subreddit: post.getAttribute('subreddit-prefixed-name'),
    score: post.getAttribute('score'),
    commentCountAttr: post.getAttribute('comment-count'),
    bodyText: bodyEl ? (bodyEl.innerText || '').trim() : ''
  };
})())
"""

_COMMENT_EXTRACT_JS = r"""
JSON.stringify(Array.from(document.querySelectorAll('shreddit-comment')).map(function(c) {
  var bodyEl = c.querySelector('[slot="comment"]');
  return {
    thingid: c.getAttribute('thingid'),
    permalink: c.getAttribute('permalink'),
    score: c.getAttribute('score'),
    created: c.getAttribute('created'),
    body: bodyEl ? (bodyEl.innerText || '').trim() : ''
  };
}))
"""

_FIND_VIEW_MORE_JS = r"""
(function() {
  function isVisible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    var style = window.getComputedStyle(el);
    if (rect.width <= 0 || rect.height <= 0) return false;
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    if (el.offsetParent === null && style.position !== 'fixed') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (el.hasAttribute('hidden')) return false;
    return true;
  }
  var candidates = Array.from(document.querySelectorAll('button')).filter(function(el) {
    var txt = (el.textContent || '').trim();
    return /view more comments/i.test(txt) && txt.length < 60;
  });
  var visibleOnes = candidates.filter(isVisible);
  if (visibleOnes.length !== 1) return JSON.stringify({found: false, count: visibleOnes.length});
  visibleOnes[0].setAttribute('data-voc-view-more', '1');
  return JSON.stringify({found: true});
})()
"""

_VIEW_MORE_SELECTOR = "[data-voc-view-more='1']"


def is_challenge(final_url: str, body_text: str) -> str | None:
    """Pure, unit-testable detector reused verbatim from what was validated
    live across every experiment this session (js_challenge URL redirect,
    the static block page's exact wording, or a CAPTCHA/human-verification
    prompt). Returns the specific CHALLENGE_REASON_* that matched, or None if
    the page shows no sign of a challenge -- a non-None string is truthy, so
    every existing `if is_challenge(...):`-shaped caller keeps working
    unchanged; only callers that want the specific reason need to read the
    return value itself."""
    low = (body_text or "").lower()
    if _CHALLENGE_URL_MARKER in (final_url or ""):
        return CHALLENGE_REASON_URL_MARKER
    if any(phrase in low for phrase in _BLOCK_PHRASES):
        return CHALLENGE_REASON_BLOCK_PHRASE
    if any(phrase in low for phrase in _CAPTCHA_PHRASES):
        return CHALLENGE_REASON_CAPTCHA_PHRASE
    return None


def classify_page(final_url: str, title: str, body_text: str) -> str | None:
    """`is_challenge` plus a defense-in-depth fallback: every observed block
    page this session had an empty <title> and a very short body (the exact
    143-char static page), while every real Reddit page had a real title --
    durable against Reddit changing its block page's wording without relying
    on that wording at all. Returns the matched CHALLENGE_REASON_*, or None."""
    reason = is_challenge(final_url, body_text)
    if reason is not None:
        return reason
    if not (title or "").strip() and len(body_text or "") < 500:
        return CHALLENGE_REASON_EMPTY_TITLE_SHORT_BODY
    return None


@dataclass(frozen=True, slots=True)
class ChallengeDetail:
    """A structured record of one challenge occurrence -- deliberately a
    fixed, named shape (not a loose dict built ad hoc at each call site) so
    every challenge captured anywhere in this module has the exact same
    fields, stable key names, and a body snippet capped the same way. Fed
    into `last_search_stats`/trace_events via `as_stats()`; the field names
    there (`challenge_reason`/`challenge_url`/`challenge_title`/
    `challenge_body_snippet`) are the stable, public contract any downstream
    consumer (diagnostics UI, future analytics/self-improving tooling) should
    read -- keep them stable, add new ones rather than renaming."""

    reason: str
    url: str
    title: str
    body_snippet: str

    def as_stats(self) -> dict[str, str]:
        return {
            "challenge_reason": self.reason,
            "challenge_url": self.url,
            "challenge_title": self.title,
            "challenge_body_snippet": self.body_snippet,
        }


def _build_challenge_detail(reason: str, final_url: str, title: str, body_text: str) -> ChallengeDetail:
    return ChallengeDetail(
        reason=reason,
        url=final_url or "",
        title=title or "",
        body_snippet=short_quote(body_text) if body_text else "",
    )


def _normalize_subreddit(raw: str) -> str:
    """Reddit's `subreddit-prefixed-name` DOM attribute returns e.g.
    "r/MechanicalKeyboards" (prefix included), but the rest of this codebase
    (PRAW's `submission.subreddit`, the scraper's `post["subreddit"]`, and
    the frontend's own `citationPrefix: "r/"` convention in sources.ts) all
    store/expect it unprefixed -- caught via the smoke test literally
    printing "r/r/keyboards"."""
    return raw[2:] if raw.lower().startswith("r/") else raw


def _parse_count(raw: Any) -> int:
    if not raw:
        return 0
    text = str(raw).strip().lower().replace(",", "")
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def rank_and_select(
    query: str, results: list[dict[str, Any]], max_posts: int, min_comment_count: int
) -> list[dict[str, Any]]:
    """v1 ranking: query relevance (lexical overlap via `simple_similarity`)
    sorted first, comment_count (discussion depth) only as the tiebreaker --
    NOT a single blended score. See the plan's §4 rationale (a high-comment,
    off-topic result must not outrank a low-comment, on-topic one)."""
    query_tokens = set(query.lower().split())
    candidates = []
    seen_urls: set[str] = set()
    for result in results:
        url = result.get("url")
        if not url or url in seen_urls:
            continue
        if result.get("promoted") or result.get("nsfw"):
            continue
        comment_count = _parse_count(result.get("commentText"))
        if comment_count < min_comment_count:
            continue
        seen_urls.add(url)
        title = result.get("title") or ""
        relevance = simple_similarity(query, title)
        subreddit = (result.get("subreddit") or "").lower()
        if subreddit and any(token and token in subreddit for token in query_tokens):
            relevance += 0.1
        candidates.append({**result, "_comment_count": comment_count, "_relevance": relevance})
    candidates.sort(key=lambda c: (-c["_relevance"], -c["_comment_count"]))
    return candidates[:max_posts]


def dedupe_comments(raw_comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for comment in raw_comments:
        key = comment.get("thingid") or f"body:{comment.get('body', '')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(comment)
    return unique


def _post_to_item(post_meta: dict[str, Any], post_url: str, post_id: str | None, subreddit: str, query: str) -> CollectedItem:
    return CollectedItem(
        source_url=post_url,
        subreddit=subreddit,
        item_type="post",
        post_id=post_id,
        comment_id=None,
        title=str(post_meta.get("title") or ""),
        body=str(post_meta.get("bodyText") or ""),
        score=_parse_count(post_meta.get("score")),
        comment_count=_parse_count(post_meta.get("commentCountAttr")),
        created_at=utc_now(),
        search_query=query,
    )


def _comment_to_item(
    comment: dict[str, Any], subreddit: str, post_id: str | None, post_title: str, query: str
) -> CollectedItem:
    permalink = str(comment.get("permalink") or "")
    source_url = f"https://www.reddit.com{permalink}" if permalink else ""
    return CollectedItem(
        source_url=source_url,
        subreddit=subreddit,
        item_type="comment",
        post_id=post_id,
        comment_id=comment.get("thingid"),
        title=post_title,
        body=str(comment.get("body") or ""),
        score=_parse_count(comment.get("score")),
        comment_count=0,
        created_at=str(comment.get("created") or utc_now()),
        search_query=query,
    )


def _default_profile_dir() -> Path:
    load_dotenv()
    configured = os.environ.get("REDDIT_CHROME_PROFILE_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".reddit-chrome-profile"


def _cdp_port() -> int:
    load_dotenv()
    raw = os.environ.get("REDDIT_CHROME_CDP_PORT", "").strip()
    return int(raw) if raw else 9222


def _env_int(name: str, default: int) -> int:
    load_dotenv()
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _allow_view_more_click() -> bool:
    load_dotenv()
    raw = os.environ.get("REDDIT_BROWSER_ALLOW_VIEW_MORE_CLICK", "true").strip().lower()
    return raw not in ("false", "0", "no")


class RedditBrowserCollector:
    """Drives a plain, independently-launched Chrome instance (managed by
    `_reddit_chrome.py`, NOT agent-browser's own managed-browser mode) via a
    CDP attach, warmed up by a one-time manual human visit. See
    `docs/reddit_browser_collector_plan.md` for the full validated
    architecture and the experiments behind it.

    Unlike AmazonCollector/YoutubeCollector, this collector does not require
    a Reddit login -- the persisted profile carries behavioral/session trust
    with Reddit's bot-detection system, not an authenticated account.
    """

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        cdp_port: int | None = None,
        max_posts_per_query: int | None = None,
        max_comments_per_post: int | None = None,
        min_comment_count: int | None = None,
        allow_view_more_click: bool | None = None,
        request_delay: float = 2.0,
    ):
        self.profile_dir = Path(profile_dir) if profile_dir is not None else _default_profile_dir()
        self.cdp_port = cdp_port if cdp_port is not None else _cdp_port()
        self.max_posts_per_query = (
            max_posts_per_query if max_posts_per_query is not None else _env_int("REDDIT_BROWSER_MAX_POSTS_PER_QUERY", 3)
        )
        self.max_comments_per_post = (
            max_comments_per_post
            if max_comments_per_post is not None
            else _env_int("REDDIT_BROWSER_MAX_COMMENTS_PER_POST", 50)
        )
        self.min_comment_count = (
            min_comment_count if min_comment_count is not None else _env_int("REDDIT_BROWSER_MIN_COMMENT_COUNT", 5)
        )
        self.allow_view_more_click = (
            allow_view_more_click if allow_view_more_click is not None else _allow_view_more_click()
        )
        self.request_delay = request_delay
        self._reddit_disabled_for_run = False
        self.last_search_stats: dict[str, Any] = {}

    def available(self) -> bool:
        try:
            locate_chrome_executable()
        except ChromeLifecycleError:
            return False
        return True

    def close(self) -> None:
        """Deliberately a no-op: the dedicated Chrome is long-lived and
        reused across runs by design (see _reddit_chrome.ensure_running).
        Shutdown is an explicit, separate maintenance operation, never tied
        to a run's lifecycle."""

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        with _SEARCH_LOCK:
            return self._search_locked(query, subreddit, limit)

    def _search_locked(self, query: str, subreddit: str, limit: int) -> list[CollectedItem]:
        start = time.monotonic()
        stats: dict[str, Any] = {
            "queries_attempted": 1,
            "search_results_discovered": 0,
            "posts_selected": 0,
            "posts_opened": 0,
            "comments_extracted": 0,
            "comments_unique": 0,
            "challenge_detected": False,
            "chrome_reused": False,
            "duration_ms": 0,
        }

        if self._reddit_disabled_for_run:
            stats["challenge_detected"] = True
            stats["chrome_reused"] = True
            stats.update(
                ChallengeDetail(reason=CHALLENGE_REASON_RUN_DISABLED, url="", title="", body_snippet="").as_stats()
            )
            self.last_search_stats = stats
            return []

        status = profile_status(self.profile_dir)
        if status == ProfileStatus.NOT_INITIALIZED:
            chrome_path = locate_chrome_executable()
            raise RuntimeError(
                "Reddit browser profile is not initialized. One-time manual setup required:\n"
                f'  "{chrome_path}" --remote-debugging-port={self.cdp_port} --user-data-dir="{self.profile_dir}"\n'
                "Then manually browse to reddit.com, open one thread with comments, confirm it loads "
                "normally (no challenge/block page), and close the window. This only needs to happen once."
            )

        _identity, chrome_reused = ensure_running(self.profile_dir, self.cdp_port)
        stats["chrome_reused"] = chrome_reused
        session = CdpSession(self.cdp_port, request_delay=self.request_delay)

        items: list[CollectedItem] = []
        search_url = self._search_url(query, subreddit)
        session.open(search_url)
        session.wait_networkidle()
        final_url = str(session.eval_json("location.href") or search_url)
        title = str(session.eval_json("document.title") or "")
        body_text = str(session.eval_json("document.body.innerText") or "")

        reason = classify_page(final_url, title, body_text)
        if reason is not None:
            detail = _build_challenge_detail(reason, final_url, title, body_text)
            self._record_challenge(status)
            stats["challenge_detected"] = True
            stats.update(detail.as_stats())
            stats["duration_ms"] = int((time.monotonic() - start) * 1000)
            self.last_search_stats = stats
            return items

        raw_results = session.eval_json(_SEARCH_EXTRACT_JS) or []
        stats["search_results_discovered"] = len(raw_results)
        selected = rank_and_select(query, raw_results, self.max_posts_per_query, self.min_comment_count)
        stats["posts_selected"] = len(selected)

        challenge_detail: ChallengeDetail | None = None
        for result in selected:
            post_items, post_challenge = self._process_post(session, result, query)
            items.extend(post_items)
            if post_items:
                stats["posts_opened"] += 1
                stats["comments_extracted"] += sum(1 for i in post_items if i.item_type == "comment")
            if post_challenge is not None:
                challenge_detail = post_challenge
                break

        stats["comments_unique"] = sum(1 for i in items if i.item_type == "comment")
        stats["duration_ms"] = int((time.monotonic() - start) * 1000)

        if challenge_detail is not None:
            self._record_challenge(status)
            stats["challenge_detected"] = True
            stats.update(challenge_detail.as_stats())
        else:
            # B2b: a clean success always resets the streak -- consecutive_challenge_count
            # is meant to reflect the current run of failures, not a lifetime total.
            updates: dict[str, Any] = {"last_success_at": utc_now(), "consecutive_challenge_count": 0}
            if status == ProfileStatus.UNKNOWN:
                updates["initialized_at"] = utc_now()
            write_profile_state(self.profile_dir, **updates)

        self.last_search_stats = stats
        return items

    def _record_challenge(self, status: ProfileStatus) -> None:
        prior_count = int(read_profile_state(self.profile_dir).get("consecutive_challenge_count") or 0)
        updates: dict[str, Any] = {
            "last_challenge_at": utc_now(),
            "consecutive_challenge_count": prior_count + 1,
        }
        if status == ProfileStatus.UNKNOWN:
            updates["initialized_at"] = utc_now()
        write_profile_state(self.profile_dir, **updates)
        self._reddit_disabled_for_run = True

    def _process_post(
        self, session: CdpSession, result: dict[str, Any], query: str
    ) -> tuple[list[CollectedItem], ChallengeDetail | None]:
        post_url = result.get("url")
        if not post_url:
            return [], None

        session.open(post_url)
        session.wait_networkidle()
        final_url = str(session.eval_json("location.href") or post_url)
        title = str(session.eval_json("document.title") or "")
        body_text = str(session.eval_json("document.body.innerText") or "")

        reason = classify_page(final_url, title, body_text)
        if reason is not None:
            return [], _build_challenge_detail(reason, final_url, title, body_text)

        post_meta = session.eval_json(_POST_META_JS)
        if not post_meta:
            return [], None  # not a challenge -- just this one post failed to render right, skip it

        subreddit = _normalize_subreddit(str(post_meta.get("subreddit") or result.get("subreddit") or "unknown"))
        post_id = result.get("postId")
        post_title = str(post_meta.get("title") or result.get("title") or "")

        items: list[CollectedItem] = [_post_to_item(post_meta, final_url, post_id, subreddit, query)]

        raw_comments = session.eval_json(_COMMENT_EXTRACT_JS) or []

        if self.allow_view_more_click:
            find_result = session.eval_json(_FIND_VIEW_MORE_JS) or {}
            if find_result.get("found"):
                session.click(_VIEW_MORE_SELECTOR)
                session.wait_networkidle()
                raw_comments = session.eval_json(_COMMENT_EXTRACT_JS) or raw_comments

        raw_comments = dedupe_comments(raw_comments)[: self.max_comments_per_post]
        for comment in raw_comments:
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            items.append(_comment_to_item(comment, subreddit, post_id, post_title, query))

        return items, None

    def _search_url(self, query: str, subreddit: str) -> str:
        encoded = quote_plus(query)
        if subreddit.strip():
            return f"https://www.reddit.com/r/{subreddit.strip()}/search/?q={encoded}&restrict_sr=on"
        return f"https://www.reddit.com/search/?q={encoded}"


def _build(context: CollectorContext) -> RedditBrowserCollector:
    return RedditBrowserCollector()


register_collector(DataSource.REDDIT, _build)
