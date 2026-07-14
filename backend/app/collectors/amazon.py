from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from ..llm import load_dotenv
from ..models import CollectedItem, DataSource
from ._agent_browser import AgentBrowserSession, parse_eval_json
from .base import CollectorContext
from .registry import register_collector

_DEFAULT_SESSION = "default"
_MAX_PRODUCTS_PER_QUERY = 3
_REQUEST_DELAY_SECONDS = 2.0

# Amazon's review-pagination widget ("Show 10 more reviews") is a client-side
# AJAX call that reliably fails after the first click or two in this automated
# session — but each star-rating filter is a fresh page navigation, not a
# repeated AJAX call, and reliably renders its own ~10 reviews with zero
# overlap with the other filters. Sweeping all five buckets and deduping by
# reviewId is the reliable way to get beyond one product's first page.
_STAR_FILTERS = ["one_star", "two_star", "three_star", "four_star", "five_star"]

# Reads structured fields straight out of Amazon's search-result DOM via
# `agent-browser eval`. Keeping this as JS run in-page (rather than parsing
# agent-browser's `read` text dump) is deliberate: Amazon's data-* attributes
# are far more stable across page redesigns than trying to regex the rendered
# text output.
_SEARCH_EXTRACT_JS = """
JSON.stringify(
  Array.from(document.querySelectorAll('[data-component-type="s-search-result"]'))
    .map(el => ({
      asin: el.getAttribute('data-asin') || null,
      title: el.querySelector('h2 span')?.textContent?.trim()
        || el.querySelector('h2')?.textContent?.trim()
        || null
    }))
)
"""

_REVIEW_EXTRACT_JS = """
JSON.stringify(
  Array.from(document.querySelectorAll('[data-hook="review"]')).map(el => ({
    reviewId: el.id || null,
    rating: el.querySelector('[data-hook="review-star-rating"] .a-icon-alt, [data-hook="cmps-review-star-rating"] .a-icon-alt')?.textContent?.trim() || null,
    title: el.querySelector('[data-hook="review-title"]')?.textContent?.trim() || null,
    date: el.querySelector('[data-hook="review-date"]')?.textContent?.trim() || null,
    body: el.querySelector('[data-hook="review-body"]')?.textContent?.trim() || null,
    helpful: el.querySelector('[data-hook="helpful-vote-statement"]')?.textContent?.trim() || null,
    verified: !!el.querySelector('[data-hook="avp-badge"]')
  }))
)
"""


def _default_profile() -> str:
    load_dotenv()
    configured = os.environ.get("AMAZON_AGENT_BROWSER_PROFILE", "").strip()
    return configured or str(Path.home() / ".agent-browser-profiles" / "amazon")


class AmazonCollector:
    """Drives a real, logged-in Chrome session (via the `agent-browser` CLI, see
    `_agent_browser.py`) to pull Amazon product reviews. Amazon has no public
    reviews API, so unlike RedditCollector this automates a browser instead of
    calling a data API.

    Requires a one-time manual login: run
        agent-browser --profile "<profile>" open https://www.amazon.com --headed
    and sign in by hand in the window that opens. `agent-browser` persists that
    login to disk at `profile`, so every later run reuses it with no further
    interaction — until Amazon's session naturally expires or automated
    behavior triggers a re-verification prompt, at which point the manual
    login step has to be repeated.

    Logged-out sessions only expose Amazon's truncated "Customers say" AI
    summary, not individual review text — signing in is what unlocks full
    reviews (reviewer, star rating, date, verified badge, body, helpful count).

    Known limitation: Amazon's in-page "Show 10 more reviews" button is a
    client-side AJAX call that reliably fails after the first click or two in
    this automated session ("There was a problem filtering reviews. Please
    reload the page.") — repeated clicking does not reliably page deeper into
    one product's reviews. Instead, `_fetch_reviews` sweeps the five
    star-rating filter URLs (each a fresh page navigation, not a repeated AJAX
    call), which reliably renders ~10 reviews per star with no overlap against
    the others, then round-robin merges the five buckets (one review per star
    per round) before deduping by reviewId and truncating to `limit`. The
    round-robin merge matters: concatenating bucket-by-bucket (one_star through
    five_star in order) meant `limit` was usually reached while still in the
    low-star buckets, so four/five-star reviews never got fetched at all and
    every report came out artificially all-negative — round-robin guarantees a
    balanced 1-5 star mix survives no matter where the truncation lands.
    """

    def __init__(
        self,
        profile: str | None = None,
        session: str = _DEFAULT_SESSION,
        max_products_per_query: int = _MAX_PRODUCTS_PER_QUERY,
        request_delay: float = _REQUEST_DELAY_SECONDS,
    ):
        self.profile = profile if profile is not None else _default_profile()
        self.max_products_per_query = max_products_per_query
        self._browser = AgentBrowserSession(self.profile, session, request_delay)

    def available(self) -> bool:
        return self._browser.available() and Path(self.profile).exists()

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        # `subreddit` has no Amazon equivalent (there's no category-scoped
        # search endpoint wired up here); every call searches all of Amazon.
        if not self.available():
            raise RuntimeError(
                "agent-browser is not installed, or no logged-in profile exists at "
                f"{self.profile}. Run `npm install -g agent-browser`, then "
                f'`agent-browser --profile "{self.profile}" open https://www.amazon.com '
                "--headed` and log in manually once."
            )
        items: list[CollectedItem] = []
        for asin, product_title in self._search_products(query):
            if len(items) >= limit:
                break
            items.extend(self._fetch_reviews(asin, product_title, query, limit - len(items)))
        return items

    def close(self) -> None:
        self._browser.close()

    def _search_products(self, query: str) -> list[tuple[str, str]]:
        self._browser.open(f"https://www.amazon.com/s?k={quote_plus(query)}")
        results = parse_eval_json(self._browser.eval(_SEARCH_EXTRACT_JS)) or []
        products: list[tuple[str, str]] = []
        seen_asins: set[str] = set()
        for entry in results:
            asin = entry.get("asin")
            # Amazon search results can list the same ASIN twice (e.g. a
            # sponsored placement plus its organic listing); without this,
            # _fetch_reviews would redundantly run the full 5-star sweep on it
            # twice — wasted navigations, not a correctness bug (downstream
            # dedup by source_url would still collapse them).
            if not asin or str(asin) in seen_asins:
                continue
            seen_asins.add(str(asin))
            products.append((str(asin), str(entry.get("title") or "")))
            if len(products) >= self.max_products_per_query:
                break
        return products

    def _fetch_reviews(self, asin: str, product_title: str, query: str, limit: int) -> list[CollectedItem]:
        # Fetch all five star-rating buckets up front (each is one fresh page
        # navigation; see _STAR_FILTERS) rather than draining one_star through
        # five_star in order. Draining in order was the original approach and it
        # systematically starved positive reviews: `limit` is usually reached
        # while still in the low-star buckets, so four/five-star content never
        # gets fetched at all and every report comes out artificially all-negative.
        batches: list[list[dict[str, Any]]] = []
        for star in _STAR_FILTERS:
            self._browser.open(f"https://www.amazon.com/portal/customer-reviews/{asin}?filterByStar={star}&reviewerType=all_reviews")
            batches.append(parse_eval_json(self._browser.eval(_REVIEW_EXTRACT_JS)) or [])

        items: list[CollectedItem] = []
        seen_review_ids: set[str] = set()
        # Round-robin merge (one review from each star bucket per round) instead
        # of concatenating bucket-by-bucket, so truncating to `limit` still keeps
        # a balanced 1-5 star mix no matter where the cutoff lands.
        for round_reviews in zip_longest(*batches):
            for review in round_reviews:
                if review is None:
                    continue
                if len(items) >= limit:
                    return items
                body = str(review.get("body") or "").strip()
                if not body:
                    continue
                review_id = _review_identity(review, body)
                if review_id in seen_review_ids:
                    continue
                item = _normalize_review(review, review_id, body, asin, product_title, query)
                seen_review_ids.add(review_id)
                items.append(item)
        return items


_RATING_RE = re.compile(r"([\d.]+)\s*out of 5")
_TITLE_RATING_PREFIX_RE = re.compile(r"^[\d.]+ out of 5 stars\s*")
_HELPFUL_COUNT_RE = re.compile(r"^([\d,]+|One)\b", re.IGNORECASE)
_REVIEW_DATE_RE = re.compile(r"on (.+)$")


def _review_identity(review: dict[str, Any], body: str) -> str:
    """Amazon's own reviewId when the DOM element has one; otherwise a stable
    hash of the review body. Without this fallback, every id-less review on a
    product collapses onto the same `source_url` (no #fragment to distinguish
    them), and react_agent's cross-call dedup (keyed on source_url) then
    silently drops all but the first as if they were duplicates of each other."""
    review_id = str(review.get("reviewId") or "").strip()
    if review_id:
        return review_id
    return "body-" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _normalize_review(review: dict[str, Any], review_id: str, body: str, asin: str, product_title: str, query: str) -> CollectedItem:
    rating_match = _RATING_RE.search(str(review.get("rating") or ""))
    rating = rating_match.group(1) if rating_match else "?"
    title = _TITLE_RATING_PREFIX_RE.sub("", str(review.get("title") or "").strip()).strip() or "(no title)"
    # Marked, not filtered: folded into `body` (rather than dropping unverified
    # reviews, or adding a dedicated CollectedItem/Evidence column) so the signal
    # reaches the LLM relevance/quality pass and is visible in report quotes,
    # without a schema change or silently losing evidence. Amazon's own "Verified
    # Purchase" badge is a much stronger authenticity signal than anything else
    # available here (there's no reviewer address exposed to compare against a
    # merchant's shipping data — Amazon never surfaces that to third parties).
    verification = "Verified Purchase" if review.get("verified") else "Not verified"
    return CollectedItem(
        source_url=f"https://www.amazon.com/product-reviews/{asin}#{review_id}",
        subreddit=(product_title.strip() or asin)[:80],
        item_type="post",
        post_id=asin,
        comment_id=review_id,
        title=title,
        body=f"Rating: {rating}/5 | {verification}\n\n{body}",
        score=_parse_helpful_count(review.get("helpful")),
        comment_count=0,
        created_at=_parse_review_date(review.get("date")),
        search_query=query,
    )


def _parse_helpful_count(raw: Any) -> int:
    match = _HELPFUL_COUNT_RE.match(str(raw or "").strip())
    if not match:
        return 0
    token = match.group(1).replace(",", "")
    if token.lower() == "one":
        return 1
    try:
        return int(token)
    except ValueError:
        return 0


def _parse_review_date(raw: Any) -> str:
    match = _REVIEW_DATE_RE.search(str(raw or ""))
    if match:
        try:
            return datetime.strptime(match.group(1).strip(), "%B %d, %Y").replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _build(context: CollectorContext) -> AmazonCollector:
    # Pinning the session to run_id isolates concurrent runs onto separate
    # browser tabs/daemons — see the AmazonCollector docstring for why that
    # matters. agent-browser sanitizes session names itself, so run_id (already
    # alphanumeric/underscore) can be passed through as-is.
    return AmazonCollector(session=context.run.run_id)


register_collector(DataSource.AMAZON, _build)
