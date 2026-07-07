from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ..llm import load_dotenv
from ..models import CollectedItem, DataSource
from .base import CollectorContext
from .registry import register_collector


def reddit_credentials() -> dict[str, str]:
    load_dotenv()
    return {
        "client_id": os.environ.get("REDDIT_CLIENT_ID", ""),
        "client_secret": os.environ.get("REDDIT_CLIENT_SECRET", ""),
        "user_agent": os.environ.get("REDDIT_USER_AGENT", "reddit-insight-agent/0.1"),
    }


class RedditCollector:
    """Wraps PRAW in read-only (app-only OAuth) mode: no Reddit user login required.

    The primary data-collection backend. Satisfies the `Collector` protocol
    (see collectors/base.py) but react_agent never imports this module directly.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
        max_comments_per_post: int = 5,
    ):
        creds = reddit_credentials()
        self.client_id = client_id if client_id is not None else creds["client_id"]
        self.client_secret = client_secret if client_secret is not None else creds["client_secret"]
        self.user_agent = user_agent if user_agent is not None else creds["user_agent"]
        self.max_comments_per_post = max_comments_per_post
        self._reddit: Any = None

    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _client(self) -> Any:
        if self._reddit is None:
            if not self.available():
                raise RuntimeError(
                    "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not configured. "
                    "Create a 'script' app at https://www.reddit.com/prefs/apps and set them in backend/.env."
                )
            import praw  # imported lazily so the module can be imported without the dependency installed

            self._reddit = praw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
            )
            self._reddit.read_only = True
        return self._reddit

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        reddit = self._client()
        target = reddit.subreddit(subreddit.strip() or "all")
        items: list[CollectedItem] = []
        for submission in target.search(query, sort="relevance", time_filter="year", limit=limit):
            items.append(self._normalize_post(submission, query))
            items.extend(self._top_comments(submission, query))
        return items

    def _normalize_post(self, submission: Any, query: str) -> CollectedItem:
        return CollectedItem(
            source_url=f"https://www.reddit.com{submission.permalink}",
            subreddit=str(submission.subreddit),
            item_type="post",
            post_id=str(submission.id),
            comment_id=None,
            title=str(submission.title or ""),
            body=str(submission.selftext or ""),
            score=int(submission.score or 0),
            comment_count=int(submission.num_comments or 0),
            created_at=_iso(submission.created_utc),
            search_query=query,
        )

    def _top_comments(self, submission: Any, query: str) -> list[CollectedItem]:
        try:
            submission.comments.replace_more(limit=0)
            comments = list(submission.comments)
        except Exception:
            return []
        comments.sort(key=lambda comment: int(getattr(comment, "score", 0) or 0), reverse=True)
        results: list[CollectedItem] = []
        for comment in comments[: self.max_comments_per_post]:
            body = str(getattr(comment, "body", "") or "")
            if not body or body in {"[deleted]", "[removed]"}:
                continue
            results.append(
                CollectedItem(
                    source_url=f"https://www.reddit.com{comment.permalink}",
                    subreddit=str(submission.subreddit),
                    item_type="comment",
                    post_id=str(submission.id),
                    comment_id=str(comment.id),
                    title=str(submission.title or ""),
                    body=body,
                    score=int(getattr(comment, "score", 0) or 0),
                    comment_count=0,
                    created_at=_iso(getattr(comment, "created_utc", submission.created_utc)),
                    search_query=query,
                )
            )
        return results


def _iso(unix_timestamp: float) -> str:
    return datetime.fromtimestamp(float(unix_timestamp), tz=UTC).replace(microsecond=0).isoformat()


def _build(context: CollectorContext) -> RedditCollector:
    return RedditCollector()


register_collector(DataSource.REDDIT_API, _build)
