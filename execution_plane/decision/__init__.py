"""Iteration & routing engine adapter (spec §10.1.2)."""

from .router import IterationRouter
from .router import RouteDecision

__all__ = ["IterationRouter", "RouteDecision"]
