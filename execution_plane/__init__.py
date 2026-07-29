"""Execution plane: adapters that wrap existing 01-10 layers (spec §11).

Every adapter does four things (spec §9.4):
  1. load input objects
  2. execute the layer logic
  3. publish output objects
  4. emit status events

The three events this package wires into a closed loop are:
  - ``EvalCompletedEvent``      (Benchmark Plane / layer 03)
  - ``AttackCompletedEvent``    (Adversarial Plane / layer 10)
  - ``LessonPromotedEvent``      (experience -> reinjection / layer 08)

See ``ClosedLoopOrchestrator`` for the 评测→决策 / 红队→决策 / 经验→回注 wiring.
"""

from .orchestrator import ClosedLoopOrchestrator
from .registry import AdapterRegistry
from .registry import default_registry
from .sdk import PlatformSDK

__all__ = [
    "AdapterRegistry",
    "ClosedLoopOrchestrator",
    "PlatformSDK",
    "default_registry",
]
