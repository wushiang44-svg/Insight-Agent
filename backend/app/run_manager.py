from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .collectors import CollectorContext, build_collector
from .llm import DeepSeekClient
from .react_agent import run_react_loop
from .storage import Storage


class RunManager:
    """Owns one background thread per active ReAct run (adapted from the demo's RuntimeController)."""

    def __init__(
        self,
        db_path: str | Path,
        llm_factory: Callable[[], DeepSeekClient] = DeepSeekClient,
    ):
        self.db_path = Path(db_path)
        self.llm_factory = llm_factory
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}

    def start_run(self, run_id: str) -> bool:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing is not None and existing.is_alive():
                return False
            stop_event = threading.Event()
            self._stop_events[run_id] = stop_event
            thread = threading.Thread(target=self._run, args=(run_id, stop_event), name=f"react-run-{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()
            return True

    def stop_run(self, run_id: str) -> bool:
        with self._lock:
            stop_event = self._stop_events.get(run_id)
            if stop_event is None:
                return False
            stop_event.set()
            return True

    def is_running(self, run_id: str) -> bool:
        thread = self._threads.get(run_id)
        return thread is not None and thread.is_alive()

    def _run(self, run_id: str, stop_event: threading.Event) -> None:
        storage = Storage(self.db_path)
        collector = None
        try:
            storage.migrate()
            run = storage.get_run(run_id)
            if run is None:
                raise ValueError(f"Unknown run: {run_id}")
            collector = build_collector(CollectorContext(run=run, storage=storage))
            llm = self.llm_factory()
            run_react_loop(run_id, storage, collector, llm, stop_event.is_set)
        except Exception:
            # run_react_loop already persists RunStatus.FAILED with the error before re-raising.
            pass
        finally:
            # Not every Collector holds a resource worth releasing (e.g. RedditCollector
            # doesn't), so `close` isn't part of the Collector protocol — duck-type it
            # instead of forcing every implementation to grow a no-op method.
            close = getattr(collector, "close", None)
            if callable(close):
                close()
            storage.close()
