"""Autonomous build orchestrator for the EOD trading platform.

Not product code. This package reads TASK_GRAPH.yaml, decides what is runnable, spawns one
fresh agent per task, and enforces the definition of done. It is deliberately dependency-light
(stdlib + PyYAML) so it works before the product's own environment exists.

Entry point: `./orch` (see orchestrator/__main__.py).
"""

__all__ = ["escalate", "graph", "prompts", "run", "state"]
