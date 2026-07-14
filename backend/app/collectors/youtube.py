from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from ..models import CollectedItem, DataSource
from ._agent_browser import AgentBrowserSession, parse_eval_json
from .base import CollectorContext
from .registry import register_collector

_DEFAULT_SESSION = "default"
_MAX_VIDEOS_PER_QUERY = 3
_REQUEST_DELAY_SECONDS = 2.0
_SCROLL_ROUNDS = 4
_SCROLL_PIXELS = 2500
_SCROLL_WAIT_SECONDS = 3.0

_SEARCH_EXTRACT_JS = """
JSON.stringify(
  Array.from(document.querySelectorAll('ytd-video-renderer')).map(el => {
    const titleEl = el.querySelector('#video-title');
    const href = titleEl?.getAttribute('href') || '';
    const match = href.match(/[?&]v=([^&]+)/);
    return {
      videoId: match ? match[1] : null,
      title: titleEl?.textContent?.trim() || null,
      channel: el.querySelector('#channel-info #text, ytd-channel-name #text')?.textContent?.trim() || null
    };
  })
)
"""

_COMMENT_EXTRACT_JS = """
JSON.stringify(
  Array.from(document.querySelectorAll('ytd-comment-thread-renderer')).map(el => ({
    author: el.querySelector('#author-text span, #author-text')?.textContent?.trim() || null,
    time: el.querySelector('.published-time-text, #published-time-text')?.textContent?.trim() || null,
    text: el.querySelector('#content-text')?.textContent?.trim() || null,
    likes: el.querySelector('#vote-count-middle')?.textContent?.trim() || null
  }))
)
"""


class YoutubeCollector:
    """Drives a real Chrome session (via the `agent-browser` CLI, see
    `_agent_browser.py`) to pull YouTube video comments. YouTube has no public
    comments API available to this project, so like AmazonCollector this
    automates a browser instead of calling a data API — but unlike Amazon,
    comments are public: no login/`--profile` is needed, an ephemeral session
    is enough.

    Comments load via lazy-scroll (YouTube's own infinite-scroll continuation
    request), not clicking a button or navigating distinct URLs like Amazon's
    star filters — `_fetch_comments` scrolls the video page `scroll_rounds`
    times with an explicit wait after each scroll, since the continuation
    request needs a few real seconds to resolve after the relevant DOM node
    scrolls into view; checking immediately after a scroll reliably finds zero
    comments even when the mechanism is working fine. This was measured
    directly: a fresh page immediately after one scroll+short-wait routinely
    read 0 comment threads, while the same page given ~15s across a few
    scroll+wait rounds reliably read 60+.

    Like AmazonCollector, every instance is pinned to its own agent-browser
    `--session` (see that class's docstring for why) — `registry.py`'s
    `_build` passes the owning run's run_id.
    """

    def __init__(
        self,
        session: str = _DEFAULT_SESSION,
        max_videos_per_query: int = _MAX_VIDEOS_PER_QUERY,
        request_delay: float = _REQUEST_DELAY_SECONDS,
        scroll_rounds: int = _SCROLL_ROUNDS,
    ):
        self.max_videos_per_query = max_videos_per_query
        self.scroll_rounds = scroll_rounds
        # profile=None: no login needed, so no persistent cookie state to keep.
        self._browser = AgentBrowserSession(None, session, request_delay)

    def available(self) -> bool:
        return self._browser.available()

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        # `subreddit` has no YouTube equivalent; every call searches all of YouTube.
        if not self.available():
            raise RuntimeError(
                "agent-browser is not installed. Run `npm install -g agent-browser`, "
                "then `agent-browser install` to download its Chrome runtime."
            )
        items: list[CollectedItem] = []
        for video_id, video_title, channel in self._search_videos(query):
            if len(items) >= limit:
                break
            items.extend(self._fetch_comments(video_id, video_title, channel, query, limit - len(items)))
        return items

    def close(self) -> None:
        self._browser.close()

    def _search_videos(self, query: str) -> list[tuple[str, str, str]]:
        self._browser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
        results = parse_eval_json(self._browser.eval(_SEARCH_EXTRACT_JS)) or []
        videos: list[tuple[str, str, str]] = []
        seen_ids: set[str] = set()
        for entry in results:
            video_id = entry.get("videoId")
            if not video_id or str(video_id) in seen_ids:
                continue
            seen_ids.add(str(video_id))
            videos.append((str(video_id), str(entry.get("title") or ""), str(entry.get("channel") or "")))
            if len(videos) >= self.max_videos_per_query:
                break
        return videos

    def _fetch_comments(self, video_id: str, video_title: str, channel: str, query: str, limit: int) -> list[CollectedItem]:
        self._browser.open(f"https://www.youtube.com/watch?v={video_id}")
        self._browser.press("Escape")  # dismiss the YouTube Premium promo overlay, if present
        for _ in range(self.scroll_rounds):
            self._browser.scroll_down(_SCROLL_PIXELS)
            time.sleep(_SCROLL_WAIT_SECONDS)
        comments = parse_eval_json(self._browser.eval(_COMMENT_EXTRACT_JS)) or []

        items: list[CollectedItem] = []
        seen_ids: set[str] = set()
        for comment in comments:
            if len(items) >= limit:
                break
            text = str(comment.get("text") or "").strip()
            if not text:
                continue
            comment_id = _comment_identity(comment, text)
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            items.append(_normalize_comment(comment, comment_id, text, video_id, video_title, channel, query))
        return items


_RELATIVE_TIME_RE = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_LIKE_COUNT_RE = re.compile(r"([\d.]+)\s*([KM]?)", re.IGNORECASE)


def _comment_identity(comment: dict[str, Any], text: str) -> str:
    """YouTube comment DOM nodes don't expose a stable native id (unlike Amazon
    reviews' element id) — every comment is identified by a hash of its
    author+text instead."""
    author = str(comment.get("author") or "").strip()
    return "yt-" + hashlib.sha1(f"{author}|{text}".encode("utf-8")).hexdigest()[:16]


def _normalize_comment(comment: dict[str, Any], comment_id: str, text: str, video_id: str, video_title: str, channel: str, query: str) -> CollectedItem:
    author = str(comment.get("author") or "").strip() or "unknown"
    return CollectedItem(
        source_url=f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
        subreddit=(video_title.strip() or video_id)[:80],
        item_type="comment",
        post_id=video_id,
        comment_id=comment_id,
        title=video_title.strip() or "(untitled video)",
        body=f"Comment by {author} on \"{video_title}\" ({channel}):\n\n{text}",
        score=_parse_like_count(comment.get("likes")),
        comment_count=0,
        created_at=_parse_relative_time(comment.get("time")),
        search_query=query,
    )


def _parse_like_count(raw: Any) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    match = _LIKE_COUNT_RE.match(text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    suffix = match.group(2).upper()
    if suffix == "K":
        number *= 1_000
    elif suffix == "M":
        number *= 1_000_000
    return int(number)


def _parse_relative_time(raw: Any) -> str:
    match = _RELATIVE_TIME_RE.search(str(raw or ""))
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta_seconds = amount * _UNIT_SECONDS[unit]
        return (datetime.now(UTC) - timedelta(seconds=delta_seconds)).replace(microsecond=0).isoformat()
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _build(context: CollectorContext) -> YoutubeCollector:
    return YoutubeCollector(session=context.run.run_id)


register_collector(DataSource.YOUTUBE, _build)
