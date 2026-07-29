"""Convergence detection for the coordinator research loop.

Monitors score velocity across experiments and detects plateaus.
When a plateau is detected, generates intervention messages that are
injected into the coordinator's context via RunExecutor tool results.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .idea_tree import IdeaTree

log = logging.getLogger(__name__)


class ConvergenceConfig(BaseModel):
    """Configuration for plateau detection thresholds."""

    enabled: bool = True
    # Minimum completed experiments before detection activates
    min_experiments: int = 4
    # Sliding window of recent experiments to consider for velocity
    window_size: int = 5
    # Relative improvement threshold: score must improve by at least
    # this fraction of |trunk_score| to count as "improving"
    improvement_threshold: float = 0.001
    # How many consecutive non-improving children under the same parent
    # triggers parent exhaustion
    parent_exhaustion_count: int = 3
    # Escalation thresholds (consecutive non-improving experiments)
    warn_after: int = 3
    force_after: int = 5
    stop_after: int = 8

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConvergenceConfig:
        known = set(cls.model_fields)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ConvergenceSignal:
    """Signal emitted when a plateau is detected."""

    level: Literal["warn", "paradigm_shift", "stop"]
    reason: str
    velocity: float
    consecutive_non_improving: int
    exhausted_parents: list[str]
    suggested_actions: list[str]


@dataclass
class ConvergenceState:
    """Internal tracking state (rebuilt from tree on each check)."""

    consecutive_non_improving: int = 0
    last_improvement_node: str | None = None
    last_improvement_score: float | None = None
    total_done: int = 0
    escalation_level: int = 0  # 0=none, 1=warn, 2=force, 3=stop


class ConvergenceDetector:
    """Detects score plateaus and generates intervention messages.

    Usage:
        detector = ConvergenceDetector(tree, config)
        signal = detector.on_experiment_complete(node_id)
        if signal:
            intervention_text = detector.format_intervention(signal)
    """

    def __init__(self, tree: IdeaTree, config: ConvergenceConfig):
        self._tree = tree
        self._config = config
        self._state = ConvergenceState()
        self._completion_order: list[str] = []

    def on_experiment_complete(self, node_id: str) -> ConvergenceSignal | None:
        """Called after a executor completes and its score is recorded.

        Returns a ConvergenceSignal if plateau is detected, else None.
        """
        if not self._config.enabled:
            return None

        trunk_score = self._tree.meta.get("trunk_score")
        if trunk_score is None:
            return None

        # Track completion order
        if node_id not in self._completion_order:
            self._completion_order.append(node_id)

        # Rebuild state from tree (handles resume after crash)
        self._rebuild_state()

        # Not enough data yet
        if self._state.total_done < self._config.min_experiments:
            return None

        # Determine escalation level
        n = self._state.consecutive_non_improving
        if n >= self._config.stop_after:
            level: Literal["warn", "paradigm_shift", "stop"] = "stop"
        elif n >= self._config.force_after:
            level = "paradigm_shift"
        elif n >= self._config.warn_after:
            level = "warn"
        else:
            return None

        # Compute velocity
        velocity = self._compute_velocity()

        # Find exhausted parents
        exhausted = self._find_exhausted_parents()

        # Build signal
        signal = ConvergenceSignal(
            level=level,
            reason=self._build_reason(n, trunk_score, velocity),
            velocity=velocity,
            consecutive_non_improving=n,
            exhausted_parents=exhausted,
            suggested_actions=self._get_suggestions(level, exhausted),
        )

        log.info(
            "Convergence signal: level=%s, consecutive_non_improving=%d, velocity=%.6f",
            level, n, velocity,
        )

        return signal

    def _rebuild_state(self) -> None:
        """Rebuild convergence state from the current tree."""
        trunk_score = self._tree.meta.get("trunk_score")
        if trunk_score is None:
            return

        # Get all completed experiments (excluding ROOT) in completion order
        done_nodes = [
            n for n in self._tree.get_all_nodes()
            if n.status in ("done", "merged")
            and n.score is not None
            and n.id != self._tree.root_id
        ]
        self._state.total_done = len(done_nodes)

        if not done_nodes:
            self._state.consecutive_non_improving = 0
            return

        # Sort by completion order if we have it, otherwise by ID
        if self._completion_order:
            order_map = {nid: i for i, nid in enumerate(self._completion_order)}
            done_nodes.sort(key=lambda n: order_map.get(n.id, 999999))
        else:
            done_nodes.sort(key=lambda n: n.id)

        # Count consecutive non-improving from the end
        consecutive = 0
        for node in reversed(done_nodes):
            if self._is_meaningful_improvement(node.score, trunk_score):
                self._state.last_improvement_node = node.id
                self._state.last_improvement_score = node.score
                break
            consecutive += 1

        self._state.consecutive_non_improving = consecutive

    def _is_meaningful_improvement(self, score: float, trunk_score: float) -> bool:
        """Check if score represents a meaningful improvement over trunk."""
        if not self._tree.is_improvement(score, trunk_score):
            return False

        # Check if the improvement exceeds threshold
        threshold = abs(trunk_score) * self._config.improvement_threshold
        if threshold == 0:
            threshold = self._config.improvement_threshold

        direction = self._tree.meta.get("metric_direction", "maximize")
        if direction == "minimize":
            delta = trunk_score - score
        else:
            delta = score - trunk_score

        return delta > threshold

    def _compute_velocity(self) -> float:
        """Compute score improvement per experiment over recent window."""
        trunk_score = self._tree.meta.get("trunk_score")
        if trunk_score is None:
            return 0.0

        done_nodes = [
            n for n in self._tree.get_all_nodes()
            if n.status in ("done", "merged")
            and n.score is not None
            and n.id != self._tree.root_id
        ]

        if len(done_nodes) < 2:
            return 0.0

        # Sort and take window
        if self._completion_order:
            order_map = {nid: i for i, nid in enumerate(self._completion_order)}
            done_nodes.sort(key=lambda n: order_map.get(n.id, 999999))
        else:
            done_nodes.sort(key=lambda n: n.id)

        window = done_nodes[-self._config.window_size:]
        if not window:
            return 0.0

        # Velocity = best improvement in window / window size
        direction = self._tree.meta.get("metric_direction", "maximize")
        improvements = []
        for node in window:
            if direction == "minimize":
                delta = trunk_score - node.score
            else:
                delta = node.score - trunk_score
            improvements.append(max(0.0, delta))

        best_improvement = max(improvements) if improvements else 0.0
        return best_improvement / len(window)

    def _find_exhausted_parents(self) -> list[str]:
        """Find parent nodes whose recent children all failed to improve trunk."""
        trunk_score = self._tree.meta.get("trunk_score")
        if trunk_score is None:
            return []

        exhausted: list[str] = []
        threshold = self._config.parent_exhaustion_count

        # Check each non-leaf node with enough children
        for node in self._tree.get_all_nodes():
            if not node.children_ids or node.id == self._tree.root_id:
                continue

            children = self._tree.get_children(node.id)
            done_children = [
                c for c in children
                if c.status in ("done", "merged") and c.score is not None
            ]

            if len(done_children) < threshold:
                continue

            # Check last N children
            recent = done_children[-threshold:]
            all_non_improving = all(
                not self._is_meaningful_improvement(c.score, trunk_score)
                for c in recent
            )

            if all_non_improving:
                exhausted.append(node.id)

        return exhausted

    def _build_reason(self, consecutive: int, trunk_score: float, velocity: float) -> str:
        direction = self._tree.meta.get("metric_direction", "maximize")
        dir_str = "higher is better" if direction == "maximize" else "lower is better"
        return (
            f"{consecutive} consecutive experiments have not meaningfully improved "
            f"the trunk score ({trunk_score:.5f}, {dir_str}). "
            f"Score velocity: {velocity:.6f} per experiment."
        )

    def _get_suggestions(self, level: str, exhausted: list[str]) -> list[str]:
        if level == "warn":
            return [
                "Switch to a fundamentally different approach family (Leap)",
                "Ensemble/blend existing diverse results (Combine)",
                "Review if current approach has hit its ceiling",
            ]
        elif level == "paradigm_shift":
            return [
                "MANDATORY: Next idea must use a different approach family",
                f"Do NOT expand these exhausted parents: {exhausted}",
                "Try: different model architecture, different methodology, or ensemble",
                "If no promising new directions exist, proceed to finalization",
            ]
        else:  # stop
            return [
                "Ensemble the best diverse candidates if not already done",
                "Run B_test evaluation on current trunk",
                "Finalize and report results",
                "Override ONLY with genuinely novel direction not yet explored",
            ]

    def format_intervention(self, signal: ConvergenceSignal) -> str:
        """Format the intervention message for injection into coordinator context."""
        if signal.level == "warn":
            header = "CONVERGENCE WARNING"
            icon = "Warning"
        elif signal.level == "paradigm_shift":
            header = "CONVERGENCE: PARADIGM SHIFT REQUIRED"
            icon = "Alert"
        else:
            header = "CONVERGENCE: STOP RECOMMENDED"
            icon = "Critical"

        lines = [
            f"## [{icon}] {header}",
            "",
            signal.reason,
            "",
        ]

        if signal.exhausted_parents:
            lines.append(f"**Exhausted parents** (do NOT expand further): {signal.exhausted_parents}")
            lines.append("")

        lines.append("**Suggested actions:**")
        for i, action in enumerate(signal.suggested_actions, 1):
            lines.append(f"{i}. {action}")

        if signal.level == "stop":
            lines.extend([
                "",
                "You may override this recommendation ONLY if you have a genuinely "
                "novel idea that is fundamentally different from all previously "
                "explored directions. If overriding, you MUST state explicitly why "
                "this idea will break the plateau.",
            ])

        return "\n".join(lines)

    def write_stop_signal(self, workspace_dir: str | None = None) -> None:
        """Write a convergence stop signal file for the launcher to detect."""
        target_dir = workspace_dir or self._tree.json_path and str(self._tree.json_path.parent)
        if not target_dir:
            return

        signal_path = Path(target_dir) / ".convergence_stop"
        data = {
            "timestamp": time.time(),
            "trunk_score": self._tree.meta.get("trunk_score"),
            "experiments_run": self._state.total_done,
            "consecutive_non_improving": self._state.consecutive_non_improving,
            "velocity": self._compute_velocity(),
        }
        try:
            signal_path.write_text(json.dumps(data, indent=2))
            log.info("Wrote convergence stop signal to %s", signal_path)
        except OSError as e:
            log.warning("Failed to write stop signal: %s", e)
