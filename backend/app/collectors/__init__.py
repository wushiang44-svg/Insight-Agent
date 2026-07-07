from __future__ import annotations

from .base import Collector, CollectorContext, CollectorFactory
from .registry import build_collector, register_collector

# Importing these modules registers their factories (see the register_collector
# call at the bottom of each). To add a new backend (Amazon, YouTube, other
# review platforms, ...): write a collectors/<name>.py implementing Collector
# and register it there, then import it here. Nothing in react_agent.py or
# run_manager.py needs to change.
from . import json_upload as _json_upload  # noqa: F401
from . import reddit as _reddit  # noqa: F401

__all__ = [
    "Collector",
    "CollectorContext",
    "CollectorFactory",
    "build_collector",
    "register_collector",
]
