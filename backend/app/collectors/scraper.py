from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests

from ..models import CollectedItem, DataSource
from .base import CollectorContext
from .registry import register_collector

_BASE_URL = "https://www.reddit.com"
_HEADERS = {"User-Agent": "reddit-insight-agent-scraper/0.1 (unofficial, no API key)"}
_DEFAULT_REQUEST_DELAY_SECONDS = 2.0


class RedditScraperCollector:
    """Pulls Reddit's public `.json` listing endpoints directly, without OAuth credentials.

    Stopgap for DataSource.REDDIT_API while the Reddit Data API application is
    pending approval. Same Collector shape as RedditCollector, so it's a drop-in
    swap via DataSource.REDDIT_SCRAPER with zero changes to react_agent.py.

    These endpoints are unauthenticated and unofficial: no SLA, far more
    aggressive rate limiting/blocking than the real API, and technically
    outside Reddit's API Terms for automated collection. Treat this as a
    temporary bridge for development/demos, not a long-term replacement —
    switch back to DataSource.REDDIT_API once credentials arrive.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        max_comments_per_post: int = 5,
        request_delay: float = _DEFAULT_REQUEST_DELAY_SECONDS,
    ):
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)
        self.max_comments_per_post = max_comments_per_post
        self._request_delay = request_delay

    def available(self) -> bool:
        return True

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        posts = self._search_posts(query, subreddit.strip(), limit)
        items: list[CollectedItem] = []
        for post in posts:
            items.append(self._normalize_post(post, query))
            items.extend(self._fetch_comments(post, query))
        return items

    def _search_posts(self, query: str, subreddit: str, limit: int) -> list[dict[str, Any]]:
        if subreddit:
            url = f"{_BASE_URL}/r/{subreddit}/search.json"
            params: dict[str, Any] = {"q": query, "restrict_sr": "on", "sort": "relevance", "t": "year", "limit": limit}
        else:
            url = f"{_BASE_URL}/search.json"
            params = {"q": query, "sort": "relevance", "t": "year", "limit": limit}
        data = self._get(url, params)
        if not data:
            return []
        return [child["data"] for child in data.get("data", {}).get("children", []) if child.get("kind") == "t3"]

    def _fetch_comments(self, post: dict[str, Any], query: str) -> list[CollectedItem]:
        post_id = post.get("id")
        if not post_id:
            return []
        data = self._get(f"{_BASE_URL}/comments/{post_id}.json", {"limit": self.max_comments_per_post, "sort": "top"})
        if not data or len(data) < 2:
            return []
        children = data[1].get("data", {}).get("children", [])
        comments = [child["data"] for child in children if child.get("kind") == "t1"]
        comments.sort(key=lambda comment: int(comment.get("score") or 0), reverse=True)
        results: list[CollectedItem] = []
        for comment in comments[: self.max_comments_per_post]:
            body = str(comment.get("body") or "")
            if not body or body in {"[deleted]", "[removed]"}:
                continue
            results.append(
                CollectedItem(
                    source_url=f"{_BASE_URL}{comment.get('permalink', '')}",
                    subreddit=str(comment.get("subreddit") or post.get("subreddit") or "unknown"),
                    item_type="comment",
                    post_id=str(post_id),
                    comment_id=str(comment.get("id") or ""),
                    title=str(post.get("title") or ""),
                    body=body,
                    score=int(comment.get("score") or 0),
                    comment_count=0,
                    created_at=_iso(comment.get("created_utc") or post.get("created_utc")),
                    search_query=query,
                )
            )
        return results

    def _normalize_post(self, post: dict[str, Any], query: str) -> CollectedItem:
        return CollectedItem(
            source_url=f"{_BASE_URL}{post.get('permalink', '')}",
            subreddit=str(post.get("subreddit") or "unknown"),
            item_type="post",
            post_id=str(post.get("id") or ""),
            comment_id=None,
            title=str(post.get("title") or ""),
            body=str(post.get("selftext") or ""),
            score=int(post.get("score") or 0),
            comment_count=int(post.get("num_comments") or 0),
            created_at=_iso(post.get("created_utc")),
            search_query=query,
        )

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        if self._request_delay:
            time.sleep(self._request_delay)
        try:
            response = self._session.get(url, params=params, timeout=10)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None


def _iso(unix_timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(unix_timestamp), tz=UTC).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return datetime.now(UTC).replace(microsecond=0).isoformat()


def _build(context: CollectorContext) -> RedditScraperCollector:
    return RedditScraperCollector()


register_collector(DataSource.REDDIT_SCRAPER, _build)
