from __future__ import annotations

from ..models import DataSource
from .base import Collector, CollectorContext, CollectorFactory

_REGISTRY: dict[DataSource, CollectorFactory] = {}


def register_collector(data_source: DataSource, factory: CollectorFactory) -> None:
    """Called once, at import time, by each collector module (see reddit.py / json_upload.py)."""
    _REGISTRY[data_source] = factory


def build_collector(context: CollectorContext) -> Collector:
    """Looks up the factory registered for `context.run.data_source` and builds a collector.

    This is the only place run_manager needs to call — it never branches on
    `data_source` itself, so adding a new collector never requires touching it.
    """
    factory = _REGISTRY.get(context.run.data_source)
    if factory is None:
        raise ValueError(f"No collector registered for data source: {context.run.data_source.value}")
    return factory(context)
