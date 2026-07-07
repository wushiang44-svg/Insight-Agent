from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..models import CollectedItem, RunRecord
from ..storage import Storage


class Collector(Protocol):
    """Abstract interface every data-collection backend must implement.

    `react_agent.run_react_loop` only ever talks to this interface — it has no
    knowledge of Reddit, PRAW, JSON uploads, or any other concrete source.
    Adding a new backend (Amazon, YouTube, other review platforms, ...) means
    writing a class that satisfies this shape; the ReAct loop and every other
    piece of business logic stays untouched.
    """

    def available(self) -> bool:
        """Whether this collector is currently usable (e.g. credentials configured)."""
        ...

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        """Return up to `limit` items relevant to `query`, optionally narrowed to `subreddit`."""
        ...


@dataclass(slots=True)
class CollectorContext:
    """Everything a collector factory needs to build a collector for one run."""

    run: RunRecord
    storage: Storage


CollectorFactory = Callable[[CollectorContext], Collector]
