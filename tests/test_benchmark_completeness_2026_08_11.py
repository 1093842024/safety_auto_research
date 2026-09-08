"""Regression tests for benchmark-task completeness work (2026-08-11).

Covers:
  * Gray-out policy (data >1 GiB / external sibling repo / LLM weights) in
    ``benchmark_tasks._availability`` and the registration-form ``available`` flag.
  * Platform sandbox-executor ``_plan`` dispatch for the four classifier
    capabilities (text / image / audio / embedding) -> correct runner script
    and data subdir.
  * The four reference runner scripts exist and are syntactically importable.

Modules are imported via their package paths (never raw importlib) so that
relative imports such as ``from ...platform_contracts.enums`` resolve correctly.
"""
from __future__ import annotations

import os
import py_compile

import pytest

from safety_auto_research.benchmark_tasks import BenchmarkTask, _availability
from safety_auto_research.benchmark_tasks import registry as bt_registry
from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
    SandboxResearchExecutor,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts", "sandbox_examples")


def _make_task(task_id: str, task_type: str = "", **kw) -> BenchmarkTask:
    """Build a real BenchmarkTask with sensible defaults for policy checks."""
    return BenchmarkTask(
        task_id=task_id,
        name=kw.get("name", task_id),
        source_project=kw.get("source_project", "x"),
        category=kw.get("category", "x"),
        modality=kw.get("modality", "x"),
        dataset_desc=kw.get("dataset_desc", ""),
        eval_metric=kw.get("eval_metric", "accuracy"),
        direction=kw.get("direction", "higher"),
        baseline=kw.get("baseline"),
        reference=kw.get("reference"),
        gates=kw.get("gates", {}),
        harness=kw.get("harness", "manual"),
        run_command=kw.get("run_command", ""),
        source_path=kw.get("source_path", ""),
        tags=kw.get("tags", []),
        supported_by_platform=kw.get("supported_by_platform", False),
        note=kw.get("note", ""),
        task_type=task_type,
        type_config=kw.get("type_config"),
        data_size_bytes=kw.get("data_size_bytes"),
        enabled=kw.get("enabled", True),
        unavailable_reason=kw.get("unavailable_reason", ""),
        data_local=kw.get("data_local", False),
    )


# --------------------------------------------------------------------------- #
# Gray-out policy                                                              #
# --------------------------------------------------------------------------- #
def test_suite_tasks_grayed_out():
    for tid in ("suite.science_agent_bench", "suite.mle_bench"):
        enabled, reason, size = _availability(_make_task(tid))
        assert enabled is False
        assert size and size > 1_000_000_000


def test_mlevolve_mle_bench_grayed_out():
    enabled, reason, size = _availability(_make_task("mlevolve.mle_bench"))
    assert enabled is False
    assert "mle-bench" in reason.lower() or "openai" in reason.lower()


def test_external_oss_prefixes_grayed_out():
    for tid in ("autolab.safety_router", "claudini.injection", "arbor.algotune_knn",
                "autoresearchclaw.arc_bench", "ara.understanding", "autoclaude.trigger_eval"):
        enabled, reason, size = _availability(_make_task(tid))
        assert enabled is False


def test_llm_task_types_grayed_in_form():
    for tt in ("llm_sft", "llm_rl", "llm_opd"):
        spec = next(s for s in bt_registry.TASK_TYPE_SPECS if s["type_id"] == tt)
        assert spec.get("available") is False
        assert "1GB" in spec.get("unavailable_reason", "")


def test_platform_and_classifier_tasks_enabled():
    # 5 platform-native tasks are enabled
    for tid in ("platform.titanic", "platform.spaceship-titanic", "platform.wine",
                "platform.iris", "platform.breast_cancer"):
        enabled, _, _ = _availability(_make_task(tid, supported_by_platform=True))
        assert enabled is True
    # 4 classifier custom types are platform-executable (not grayed by availability)
    for tt in ("text_classification", "image_classification",
               "audio_classification", "embedding_contrastive"):
        spec = next(s for s in bt_registry.TASK_TYPE_SPECS if s["type_id"] == tt)
        assert spec["executable"] is True
        assert spec["harness"].endswith("_sandbox")


def test_data_local_task_over_1gib_is_grayed_out():
    """The 1 GiB policy must apply to *locally materialised* data too — a data_local
    task that declares a lower bound over the threshold cannot slip through as
    enabled just because the materialisation path exists (review 2026-09-08 N3)."""
    enabled, reason, size = _availability(
        _make_task("custom.huge", data_local=True, data_size_bytes=(1 << 30) + 1)
    )
    assert enabled is False
    assert "1GB" in reason
    assert size == (1 << 30) + 1


def test_data_local_task_within_1gib_is_enabled():
    """At or below the threshold (and when the size is unknown), data_local wins."""
    for size in (1 << 30, (1 << 30) - 1, 0, None):
        enabled, reason, _ = _availability(
            _make_task("custom.small", data_local=True, data_size_bytes=size)
        )
        assert enabled is True, (size, reason)


# --------------------------------------------------------------------------- #
# Platform sandbox-executor dispatch                                          #
# --------------------------------------------------------------------------- #
def test_sandbox_executor_dispatch_routes_to_runners():
    executor = SandboxResearchExecutor()

    cap_to_script = {
        "text_cls_sandbox": "run_text_cls_sandbox.py",
        "image_cls_sandbox": "run_image_cls_sandbox.py",
        "audio_cls_sandbox": "run_audio_cls_sandbox.py",
        "embedding_sandbox": "run_embedding_sandbox.py",
    }
    subdir = {"text_cls_sandbox": "text_cls_demo", "image_cls_sandbox": "image_cls_demo",
              "audio_cls_sandbox": "audio_cls_demo", "embedding_sandbox": "embedding_demo"}
    stage_run = type("R", (), {"run_id": "t"})()

    for cap, script in cap_to_script.items():
        params = {"_capability_id": cap, "_objective_snapshot": {"direction": "higher"}}
        cmd, data_dir, scratch, result_name, meta = executor._plan(stage_run, params)
        cmd_str = " ".join(cmd)
        assert script in cmd_str, f"{cap} did not route to {script}: {cmd_str}"
        assert subdir[cap] in cmd_str, f"{cap} missing subdir {subdir[cap]}: {cmd_str}"


# --------------------------------------------------------------------------- #
# Runner scripts exist + parseable                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [
    "run_text_cls_sandbox.py", "run_image_cls_sandbox.py",
    "run_audio_cls_sandbox.py", "run_embedding_sandbox.py",
])
def test_runner_script_parseable(script):
    path = os.path.join(SCRIPTS, script)
    assert os.path.exists(path), f"missing runner script: {path}"
    # importing the module should not fail (torch import is guarded at call time for
    # some; at module load text/image/audio/embedding import torch — just assert the
    # file is syntactically importable by compiling it).
    py_compile.compile(path, doraise=True)


def test_image_sample_data_is_flat_imagefolder():
    """Regression: image sample data must be a flat ImageFolder (<root>/<class>/*.png),
    not nested under train/val (which ImageFolder would misread as the two classes)."""
    img_dir = os.path.join(REPO, "benchmark_tasks", "sample_data", "image_cls_demo")
    assert os.path.isdir(img_dir)
    # top-level entries must be class dirs (no 'train'/'val' split dirs)
    top = [d for d in os.listdir(img_dir) if os.path.isdir(os.path.join(img_dir, d))]
    assert "train" not in top and "val" not in top
    assert len(top) >= 2  # at least 2 classes
    # each class dir contains pngs
    n_png = sum(len([f for f in os.listdir(os.path.join(img_dir, c)) if f.endswith(".png")])
               for c in top)
    assert n_png >= 10
