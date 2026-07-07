from __future__ import annotations

from pathlib import Path

from app.collectors.json_upload import JsonUploadCollector
from app.llm import DeepSeekClient
from app.models import CollectedItem, DataSource, RunStatus
from app.react_agent import run_react_loop
from app.storage import Storage


def make_item(url: str, title: str, body: str, subreddit: str = "gadgets", score: int = 10) -> CollectedItem:
    return CollectedItem(
        source_url=url,
        subreddit=subreddit,
        item_type="post",
        post_id=url,
        comment_id=None,
        title=title,
        body=body,
        score=score,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        search_query="test query",
    )


NOISE_ITEM = make_item("https://reddit.com/noise", "Random post", "Went for a walk today and had a sandwich.")

PAIN_ITEM_1 = make_item("https://reddit.com/pain1", "Battery issue", "I hate how fast the battery dies on this thing.")
PAIN_ITEM_2 = make_item("https://reddit.com/pain2", "Battery issue again", "The battery life is terrible, so annoying.")


class FakeCollector:
    """Duck-types the Collector interface: one canned batch of items per successive `search()` call."""

    def __init__(self, batches: list[list[CollectedItem]]):
        self.batches = batches
        self.calls = 0

    def available(self) -> bool:
        return True

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        batch = self.batches[self.calls] if self.calls < len(self.batches) else []
        self.calls += 1
        return batch


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


def run_loop(tmp_path: Path, batches: list[list[CollectedItem]], max_iterations: int, min_evidence_target: int) -> tuple[Storage, str]:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    run = storage.create_run(
        product_category="wireless earbuds",
        keywords=["earbuds"],
        target_subreddits=[],
        max_iterations=max_iterations,
        min_evidence_target=min_evidence_target,
    )
    collector = FakeCollector(batches)
    run_react_loop(run.run_id, storage, collector, no_llm(), should_stop=lambda: False)
    return storage, run.run_id


def test_stops_at_iteration_cap_when_target_never_reached(tmp_path: Path) -> None:
    batches = [[PAIN_ITEM_1], [PAIN_ITEM_2], [NOISE_ITEM]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=3, min_evidence_target=1000)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.iteration_count == 3

    events = storage.list_trace_events(run_id)
    sufficiency_events = [event for event in events if event.step_type.value == "sufficiency_check"]
    assert len(sufficiency_events) == 3
    assert "最大搜索轮次" in sufficiency_events[-1].message
    assert storage.get_report(run_id) is not None


def test_stops_early_on_diminishing_returns(tmp_path: Path) -> None:
    batches = [[NOISE_ITEM], [NOISE_ITEM]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=5, min_evidence_target=100)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.iteration_count == 2  # stopped well before max_iterations=5

    events = storage.list_trace_events(run_id)
    sufficiency_events = [event for event in events if event.step_type.value == "sufficiency_check"]
    assert sufficiency_events[0].payload["sufficient"] is False
    assert sufficiency_events[-1].payload["sufficient"] is True
    assert "收益递减" in sufficiency_events[-1].message


def test_deduplicates_evidence_by_url_across_iterations(tmp_path: Path) -> None:
    new_item = make_item("https://reddit.com/pain3", "New battery complaint", "Battery drains so fast, I hate it.")
    batches = [[PAIN_ITEM_1], [PAIN_ITEM_1, new_item]]  # PAIN_ITEM_1 repeated
    storage, run_id = run_loop(tmp_path, batches, max_iterations=2, min_evidence_target=1)

    evidence = storage.list_evidence(run_id)
    urls = [item.source_url for item in evidence]
    assert urls.count("https://reddit.com/pain1") == 1
    assert "https://reddit.com/pain3" in urls


def test_json_upload_collector_drives_the_same_loop_to_a_non_empty_report(tmp_path: Path) -> None:
    """Proves the data source is pluggable: swapping FakeReddit for the real
    JsonUploadCollector runs the identical run_react_loop unmodified."""
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    run = storage.create_run(
        product_category="wireless earbuds",
        keywords=[],
        target_subreddits=[],
        max_iterations=5,
        min_evidence_target=2,
        data_source=DataSource.JSON_UPLOAD,
    )
    uploaded_items = [PAIN_ITEM_1, PAIN_ITEM_2, make_item("https://reddit.com/pain3", "Battery again", "Battery hate it, dies fast.", subreddit="earbuds")]
    run_react_loop(run.run_id, storage, JsonUploadCollector(uploaded_items), no_llm(), should_stop=lambda: False)

    finished = storage.get_run(run.run_id)
    assert finished.status == RunStatus.COMPLETED
    assert len(storage.list_evidence(run.run_id)) == 3
    report = storage.get_report(run.run_id)
    assert report is not None
    assert report.top_pain_points  # non-empty: this data source actually produced usable evidence


def test_requires_subreddit_diversity_even_after_evidence_floor_met(tmp_path: Path) -> None:
    same_subreddit_pair = [
        make_item("https://reddit.com/a", "Battery complaint A", "Battery life is terrible, hate it.", subreddit="gadgets"),
        make_item("https://reddit.com/b", "Battery complaint B", "Battery dies fast, so annoying.", subreddit="gadgets"),
    ]
    other_subreddit_item = make_item(
        "https://reddit.com/c", "Battery complaint C", "Battery is bad, hate this product.", subreddit="earbuds"
    )
    batches = [same_subreddit_pair, [other_subreddit_item]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=3, min_evidence_target=2)

    run = storage.get_run(run_id)
    assert run.iteration_count == 2  # floor met after iteration 1 but not sufficient until 2nd subreddit appears
    assert run.status == RunStatus.COMPLETED
