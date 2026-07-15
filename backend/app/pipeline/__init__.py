"""Customer Demand Intelligence Pipeline stages, one module per stage.

Phase 1: claims.py (atomic claim extraction). Later phases add screening.py,
context.py, reliability.py, dedup_cluster.py, demand.py, ranking.py, and
deep_research.py here, each following the same
`llm.available() -> try -> except -> fallback` shape used throughout react_agent.py.
"""

from __future__ import annotations
