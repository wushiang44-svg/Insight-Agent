from __future__ import annotations

from pathlib import Path

from app.collectors import CollectorContext, build_collector, registry as registry_module
from app.collectors.json_upload import JsonUploadCollector
from app.collectors.reddit import RedditCollector
from app.collectors.scraper import RedditScraperCollector
from app.models import CollectedItem, DataSource
from app.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    return storage


def test_build_collector_dispatches_reddit_api(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.REDDIT_API)

    collector = build_collector(CollectorContext(run=run, storage=storage))

    assert isinstance(collector, RedditCollector)


def test_build_collector_dispatches_reddit_scraper(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.REDDIT_SCRAPER)

    collector = build_collector(CollectorContext(run=run, storage=storage))

    assert isinstance(collector, RedditScraperCollector)


def test_build_collector_dispatches_json_upload(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.JSON_UPLOAD)
    storage.save_uploaded_items(run.run_id, [{"title": "t", "body": "b"}])

    collector = build_collector(CollectorContext(run=run, storage=storage))

    assert isinstance(collector, JsonUploadCollector)


def test_registering_a_new_collector_requires_no_changes_to_dispatch_logic(tmp_path: Path) -> None:
    """Proves the extensibility point the architecture is built for: build_collector
    never branches on data_source itself, it only looks the factory up. Swapping in a
    brand-new implementation (standing in for a future Amazon/YouTube collector) just
    works, with zero changes to react_agent.py, run_manager.py, or build_collector."""

    class FakeFutureCollector:
        def available(self) -> bool:
            return True

        def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
            return []

    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.REDDIT_API)

    original_factory = registry_module._REGISTRY[DataSource.REDDIT_API]
    registry_module.register_collector(DataSource.REDDIT_API, lambda context: FakeFutureCollector())
    try:
        collector = build_collector(CollectorContext(run=run, storage=storage))
        assert isinstance(collector, FakeFutureCollector)
    finally:
        registry_module.register_collector(DataSource.REDDIT_API, original_factory)


def test_build_collector_raises_for_unregistered_data_source(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.REDDIT_API)

    original_factory = registry_module._REGISTRY.pop(DataSource.REDDIT_API)
    try:
        try:
            build_collector(CollectorContext(run=run, storage=storage))
            assert False, "expected ValueError for an unregistered data source"
        except ValueError as exc:
            assert "reddit_api" in str(exc)
    finally:
        registry_module.register_collector(DataSource.REDDIT_API, original_factory)
