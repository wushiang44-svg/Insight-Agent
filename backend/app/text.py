from __future__ import annotations

import re
from collections import Counter


# Used only as the no-LLM fallback path (DEEPSEEK_API_KEY not configured).

ASPECT_PATTERNS: dict[str, re.Pattern[str]] = {
    "battery": re.compile(r"\b(battery|charge|charging|runtime|dies? fast)\b", re.I),
    "durability": re.compile(r"\b(broke|broken|durab|fell apart|cracked|snapped|wore out)\b", re.I),
    "price": re.compile(r"\b(price|expensive|overpriced|cheap|worth it|cost)\b", re.I),
    "shipping": re.compile(r"\b(shipping|delivery|arrived|package|shipped late)\b", re.I),
    "customer_service": re.compile(r"\b(customer service|support|warranty|refund|return policy)\b", re.I),
    "ease_of_use": re.compile(r"\b(easy to use|hard to use|confusing|intuitive|setup|instructions)\b", re.I),
    "packaging": re.compile(r"\b(packaging|box|unboxing)\b", re.I),
    "quality": re.compile(r"\b(quality|well made|flimsy|sturdy|cheaply made)\b", re.I),
    "comfort": re.compile(r"\b(comfort|comfortable|uncomfortable|fit|ergonomic)\b", re.I),
    "size": re.compile(r"\b(too big|too small|size|bulky|compact)\b", re.I),
}

POSITIVE_PATTERNS = re.compile(
    r"\b(love|great|excellent|amazing|perfect|works well|highly recommend|best)\b", re.I
)
NEGATIVE_PATTERNS = re.compile(
    r"\b(hate|terrible|awful|disappointed|waste of money|worst|regret|annoying|frustrat)\b", re.I
)

FEATURE_REQUEST_PATTERNS = re.compile(
    r"\b(wish it had|i wish|would be nice if|should have|needs? a|why doesn'?t it)\b", re.I
)
COMPARISON_PATTERNS = re.compile(
    r"\b(vs\.?|versus|compared to|better than|instead of|alternative to|switched from)\b", re.I
)

STOPWORDS = {
    "about", "after", "again", "anyone", "because", "being", "better", "could",
    "does", "from", "have", "help", "just", "like", "need", "people", "really",
    "should", "some", "that", "there", "this", "tired", "using", "want", "what",
    "when", "where", "which", "with", "without", "would", "product", "reddit",
}


def detect_aspects(text: str) -> list[str]:
    return [name for name, pattern in ASPECT_PATTERNS.items() if pattern.search(text)]


def detect_sentiment(text: str) -> str:
    positive = bool(POSITIVE_PATTERNS.search(text))
    negative = bool(NEGATIVE_PATTERNS.search(text))
    if negative and not positive:
        return "negative"
    if positive and not negative:
        return "positive"
    return "neutral"


def classify_insight_type(text: str) -> str:
    if FEATURE_REQUEST_PATTERNS.search(text):
        return "feature_request"
    if COMPARISON_PATTERNS.search(text):
        return "comparison"
    sentiment = detect_sentiment(text)
    if sentiment == "negative":
        return "pain_point"
    if sentiment == "positive":
        return "praise"
    return "question" if "?" in text else "noise"


def keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    terms = [word for word in words if word not in STOPWORDS]
    return [word for word, _ in Counter(terms).most_common(limit)]


def simple_similarity(left: str, right: str) -> float:
    left_terms = set(keywords(left, limit=20))
    right_terms = set(keywords(right, limit=20))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def short_quote(body: str, limit: int = 220) -> str:
    text = " ".join(body.split())
    return text[:limit] + ("..." if len(text) > limit else "")
