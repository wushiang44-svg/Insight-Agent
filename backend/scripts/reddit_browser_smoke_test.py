"""Real-browser smoke test for RedditBrowserCollector.

NOT part of the normal pytest suite (pyproject.toml's testpaths=["tests"]
never looks in scripts/) -- this drives a real, already-initialized Chrome
profile and makes real requests to reddit.com. Run manually:

    cd backend
    .venv/Scripts/python.exe scripts/reddit_browser_smoke_test.py

Requires REDDIT_CHROME_PROFILE_DIR (in backend/.env or the environment) to
point at a profile that has already completed the one-time manual
initialization described in RedditBrowserCollector's docstring -- this
script does not perform that initialization itself, and will fail with a
clear RuntimeError if pointed at a fresh, uninitialized profile.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.reddit_browser import RedditBrowserCollector  # noqa: E402


def main() -> int:
    collector = RedditBrowserCollector()
    print(f"Profile dir: {collector.profile_dir}")
    print(f"CDP port:    {collector.cdp_port}")
    print(f"Max posts/query: {collector.max_posts_per_query}  Max comments/post: {collector.max_comments_per_post}")
    print()

    if not collector.available():
        print("FAIL: collector.available() is False (Chrome executable not found).")
        return 1

    query = "mechanical keyboard"
    print(f'Running search("{query}") ...')
    try:
        items = collector.search(query)
    except RuntimeError as exc:
        print(f"FAIL: search() raised RuntimeError: {exc}")
        return 1

    stats = collector.last_search_stats
    print()
    print("=== last_search_stats ===")
    print(json.dumps(stats, indent=2))

    posts = [i for i in items if i.item_type == "post"]
    comments = [i for i in items if i.item_type == "comment"]
    print()
    print(f"Posts collected:    {len(posts)}")
    print(f"Comments collected: {len(comments)}")

    if stats.get("challenge_detected"):
        print()
        print("RESULT: CHALLENGE DETECTED — this is a valid, non-crashing outcome per the")
        print("collector's design (no bypass attempted), but it means this run did not")
        print("reach real Reddit content. Check the profile's health.")
        return 1

    if not items:
        print()
        print("RESULT: FAIL — no challenge detected, but zero items were collected.")
        return 1

    print()
    print("Sample comment:")
    for item in comments[:1]:
        print(f"  subreddit=r/{item.subreddit}  score={item.score}")
        print(f"  {item.body[:200]!r}")

    print()
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
