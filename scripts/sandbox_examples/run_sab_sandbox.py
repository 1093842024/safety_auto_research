#!/usr/bin/env python3
"""Sandbox runner for ScienceAgentBench (SAB) lightweight agent-eval tasks.

Generalised from the #92 representative: given ``--task <id>`` (e.g.
``sab.hrv_analyze_34``) it resolves that instance's dataset directory, official
gold ``solve.py`` (reference program standing in for an agent's output) and
official ``run_eval.py`` grader from the read-only SAB vendor corpus mounted at
``/repo``, symlinks the (small, per-task) dataset + gold reference into a
writable scratch workdir, re-executes the gold program (genuine measurement,
not a cached-file replay) and grades it.

The heavy 3.7 GB vendor corpus stays read-only in ``/repo``; only the handful of
KB-sized dataset files per task are symlinked, so no large copy is needed.

Hard isolation: runs with ``--network none`` (no egress). If a task's gold
program needs a library beyond numpy/pandas/scikit-learn/scipy (e.g. rdkit,
neurokit2, biopsykit, ccobra, matminer, geopandas, MDAnalysis, cftime/iris) the
import fails -> the runner still writes ``result.json`` with ``passed=False``
and a ``port_notes`` entry naming the missing dependency, so the launch
completes honestly instead of crashing the executor without a result file.

Output contract (written to ``$AGENT_SCRATCH_DIR/result.json``):

    {"task_id": "<id>", "eval_metric": "max_abs_error", "threshold": 1e-4,
     "op": "le", "passed": <bool>, "gate_passed": <bool>,
     "metrics": {"primary": <float>, "max_abs_error": <float>,
                 "success": <float>, "keys_match": <float>, "sab_detail": "<str>"},
     "isolation": {"data_readonly": true, "network_blocked": true},
     "port_notes": ["<note>"]}

Usage (inside the sandbox):
    python /repo/scripts/sandbox_examples/run_sab_sandbox.py --data-dir /data/ --task <id>
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths (env-overridable; the host launcher sets AGENT_REPO_DIR / AGENT_DATA_DIR
# / AGENT_SCRATCH_DIR and mounts them RO/RW into the container).
# --------------------------------------------------------------------------- #
_REPO = os.environ.get("AGENT_REPO_DIR", "/repo")
_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/data")
_SCRATCH = os.environ.get("AGENT_SCRATCH_DIR", "/scratch")

_VENDOR = os.path.join(
    _REPO, "benchmark_tasks", "suites", "data", "vendor",
    "ScienceAgentBench", "benchmark",
)
_INDEX = os.path.join(
    _REPO, "experiments", "oss_validation", "sab", "sab_task_index.json",
)

TOLERANCE = 1e-4

# --------------------------------------------------------------------------- #
# Per-task registry. Derived from experiments/oss_validation/sab/sab_task_index.json
# (the 20 lightweight SAB instances: #92 already wired + the 19 remaining). Each
# entry: instance_id, gold_program_name, eval_script_name, output_fname, domain,
# and the third-party deps the gold/eval programs pull in (used only for the
# honest env-blocked note when those libs are absent from the sandbox image).
# --------------------------------------------------------------------------- #
TASKS = {
    "sab.mat_feature_select_2": {"iid": 2, "gp": "mat_feature_select.py", "es": "eval_mat_feature_select.py", "out": "pred_results/mat_diffusion_features.csv", "domain": "Computational Chemistry", "deps": ["mastml"]},
    "sab.predict_bulk_modulus_3": {"iid": 3, "gp": "predict_bulk_modulus.py", "es": "eval_bulk_modulus.py", "out": "pred_results/compound_bulk_modulus.csv", "domain": "Computational Chemistry", "deps": ["matminer"]},
    "sab.dili_models_ecfp_rf_18": {"iid": 18, "gp": "DILI_models_ECFP_RF.py", "es": "eval_DILI_RF.py", "out": "pred_results/MCNC_RF.csv", "domain": "Bioinformatics", "deps": ["rdkit"]},
    "sab.dili_models_ecfp_svm_19": {"iid": 19, "gp": "DILI_models_ECFP_SVM.py", "es": "eval_DILI_SVM.py", "out": "pred_results/MCNC_SVM.csv", "domain": "Bioinformatics", "deps": ["rdkit"]},
    "sab.deforestation_21": {"iid": 21, "gp": "deforestation.py", "es": "eval_deforestation.py", "out": "pred_results/deforestation_rate.csv", "domain": "Geographical Information Science", "deps": ["geopandas"]},
    "sab.ligand_fingerprint_26": {"iid": 26, "gp": "ligand_fingerprint.py", "es": "eval_fingerprint.py", "out": "pred_results/ligand_fingerprint_pred.csv", "domain": "Computational Chemistry", "deps": ["MDAnalysis", "prolif"]},
    "sab.bio_eventrelated_analyze_29": {"iid": 29, "gp": "bio_eventrelated_analyze.py", "es": "eval_bio_eventrelated_analyze.py", "out": "pred_results/bio_eventrelated_100hz_analysis_pred.csv", "domain": "Psychology and Cognitive science", "deps": ["neurokit2"]},
    "sab.hrv_analyze_34": {"iid": 34, "gp": "HRV_analyze.py", "es": "eval_HRV_analyze.py", "out": "pred_results/hrv_analysis_pred.csv", "domain": "Psychology and Cognitive science", "deps": ["neurokit2"]},
    "sab.rrv_analyze_35": {"iid": 35, "gp": "RRV_analyze.py", "es": "eval_RRV_analyze.py", "out": "pred_results/rrv_analysis_pred.csv", "domain": "Psychology and Cognitive science", "deps": ["neurokit2"]},
    "sab.cft_37": {"iid": 37, "gp": "cft.py", "es": "eval_biopsykit_cft.py", "out": "pred_results/cft_pred_results.json", "domain": "Psychology and Cognitive science", "deps": ["biopsykit"]},
    "sab.md_rf_40": {"iid": 40, "gp": "MD_RF.py", "es": "eval_MD_RF.py", "out": "pred_results/MD_MCNC_RF.csv", "domain": "Bioinformatics", "deps": ["rdkit"]},
    "sab.md_knn_41": {"iid": 41, "gp": "MD_KNN.py", "es": "eval_MD_KNN.py", "out": "pred_results/MD_MCNC_KNN.csv", "domain": "Bioinformatics", "deps": ["rdkit"]},
    "sab.imu_44": {"iid": 44, "gp": "imu.py", "es": "biopsykit_imu_eval.py", "out": "pred_results/imu_pred.json", "domain": "Psychology and Cognitive science", "deps": ["biopsykit"]},
    "sab.questionnaire_45": {"iid": 45, "gp": "questionnaire.py", "es": "biopsykit_questionnaire_eval.py", "out": "pred_results/questionnaire_pred.csv", "domain": "Psychology and Cognitive science", "deps": ["biopsykit"]},
    "sab.nvc_gen_ind_58": {"iid": 58, "gp": "nvc_gen_ind.py", "es": "eval_syllogistic_nvc_gen_ind.py", "out": "pred_results/individuals.csv", "domain": "Psychology and Cognitive science", "deps": ["ccobra"]},
    "sab.nvc_accuracies_60": {"iid": 60, "gp": "nvc_accuracies.py", "es": "eval_syllogistic_nvc_accuracies.py", "out": "pred_results/accuracies.csv", "domain": "Psychology and Cognitive science", "deps": ["ccobra"]},
    "sab.cogsci_pattern_high_sim_67": {"iid": 67, "gp": "CogSci_pattern_high_sim.py", "es": "CogSci_pattern_high_sim_eval.py", "out": "pred_results/CogSci_pattern_high_sim_data_pred.csv", "domain": "Psychology and Cognitive science", "deps": ["ccobra"]},
    "sab.saliva_85": {"iid": 85, "gp": "saliva.py", "es": "biopsykit_saliva_eval.py", "out": "pred_results/saliva_pred.json", "domain": "Bioinformatics", "deps": ["biopsykit"]},
    "sab.polynomial_fit_87": {"iid": 87, "gp": "polynomial_fit.py", "es": "eval_polynomial_fit.py", "out": "pred_results/polynomial_fit_pred.csv", "domain": "Geographical Information Science", "deps": ["cftime", "iris"]},
    # already-wired representative (kept for completeness / backward-compat)
    "sab.h_importances_92": {"iid": 92, "gp": "h_importances.py", "es": "eval_h_importances.py", "out": "pred_results/jnmf_h_importances.json", "domain": "Psychology and Cognitive science", "deps": []},
}


# --------------------------------------------------------------------------- #
# Helpers (mirror experiments/oss_validation/sab/materialize_and_eval.py)
# --------------------------------------------------------------------------- #
def extract_gold_pred(es_src: str):
    """(gold_basename, pred_basename) as referenced inside the eval script."""
    gm = re.search(r"gold_results/([^\"'\s\)]+)", es_src)
    pm = re.search(r"pred_results/([^\"'\s\)]+)", es_src)
    return (gm.group(1) if gm else None), (pm.group(1) if pm else None)


def extract_dataset_roots(gp_src: str):
    roots = set()
    for m in re.finditer(r"benchmark/datasets/([A-Za-z0-9_\-\.]+)", gp_src):
        roots.add(m.group(1))
    for m in re.finditer(r"benchmark\.datasets\.([A-Za-z0-9_]+)", gp_src):
        roots.add(m.group(1))
    return roots


def probe_readonly(data_dir: str) -> bool:
    probe = os.path.join(data_dir, ".write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def probe_network_blocked() -> bool:
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=2):
            return False
    except OSError:
        return True


def safe_symlink(src: str, dst: str) -> None:
    if os.path.islink(dst) or os.path.exists(dst):
        return
    os.symlink(src, dst)


def resolve_task(task_id: str) -> dict:
    """Return the task metadata dict, falling back to the on-disk index."""
    if task_id in TASKS:
        return TASKS[task_id]
    # Fallback: build from the SAB task index if present in the repo.
    if os.path.exists(_INDEX):
        idx = json.loads(Path(_INDEX).read_text())
        by_id = {t["instance_id"]: t for t in idx["tasks"]}
        # derive instance id from the trailing _<n> in the task_id
        m = re.search(r"_(\d+)$", task_id)
        if m:
            t = by_id.get(int(m.group(1)))
            if t:
                return {
                    "iid": t["instance_id"],
                    "gp": t["gold_program_name"],
                    "es": t["eval_script_name"],
                    "out": t.get("output_fname"),
                    "domain": t.get("domain", ""),
                    "deps": [],
                }
    raise KeyError(f"unknown SAB task id: {task_id}")


# --------------------------------------------------------------------------- #
# Materialise (symlink) the per-task scratch workspace from the read-only vendor
# --------------------------------------------------------------------------- #
def apply_compat_shims(gp_src: str, task_id: str) -> str:
    """Apply minimal, behavior-preserving compatibility shims so the official
    gold program runs under the modern dependency set baked into the sandbox
    image. These never alter the intended computation:

      * sklearn ``GridSearchCV(..., n_jobs=5, cv=<generator>)`` makes joblib's
        loky backend try to pickle the generator ``cv`` -> ``PicklingError``.
        ``n_jobs=1`` runs the CV folds inline (no worker process, no pickling)
        and yields identical results for these deterministic estimators
        (fixed ``random_state`` / seeded splits).

    Vendor gold sources stay pristine; the shim is applied only to the in-memory
    copy written to scratch, so the transformation is fully reversible.
    """
    src = gp_src
    # GridSearchCV with n_jobs>1 + a generator `cv` -> joblib pickle error.
    src = re.sub(r"n_jobs\s*=\s*5\b", "n_jobs=1", src)
    # Defensive: also neutralise n_jobs=-1 / n_jobs=2..9 (none currently present).
    src = re.sub(r"n_jobs\s*=\s*-1\b", "n_jobs=1", src)
    return src


def materialize(task_id: str, meta: dict, work: Path, notes: list[str]) -> bool:
    """Build work/{solve.py, run_eval.py, benchmark/, gold/}. Returns False if a
    required source file is missing."""
    gp = meta["gp"]
    es = meta["es"]
    gp_src_path = Path(_VENDOR) / "gold_programs" / gp
    es_src_path = Path(_VENDOR) / "eval_programs" / es
    if not gp_src_path.exists():
        notes.append(f"missing gold program in vendor corpus: {gp}")
        return False
    if not es_src_path.exists():
        notes.append(f"missing eval program in vendor corpus: {es}")
        return False

    gp_src = gp_src_path.read_text()
    es_src = es_src_path.read_text()

    # --- datasets (symlink each referenced root into benchmark/datasets) ------
    ds_root = work / "benchmark" / "datasets"
    ds_root.mkdir(parents=True, exist_ok=True)
    for d in extract_dataset_roots(gp_src):
        src = Path(_VENDOR) / "datasets" / d
        if not src.exists():
            notes.append(f"dataset root missing in vendor corpus: {d}")
            continue
        safe_symlink(str(src), str(ds_root / d))
    for p in (work / "benchmark" / "__init__.py", ds_root / "__init__.py"):
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("")

    # --- gold reference output (symlink into gold/, patch eval path) ---------
    gold_name, _pred_name = extract_gold_pred(es_src)
    gold_dir = work / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    if gold_name:
        gsrc = Path(_VENDOR) / "eval_programs" / "gold_results" / gold_name
        if gsrc.exists():
            safe_symlink(str(gsrc), str(gold_dir / gold_name))
        else:
            notes.append(f"gold reference output missing in vendor corpus: {gold_name}")

    # write run_eval.py with the gold path patched to the local gold/ dir
    patched = re.sub(r"(?:\./)?benchmark/eval_programs/gold_results/", "gold/", es_src)
    (work / "run_eval.py").write_text(patched)

    # copy the reference gold program as solve.py (with compat shims applied)
    (work / "solve.py").write_text(apply_compat_shims(gp_src, task_id))
    return True


# --------------------------------------------------------------------------- #
# Execute + grade
# --------------------------------------------------------------------------- #
def run_solve(work: Path) -> dict:
    (work / "pred_results").mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [sys.executable, "solve.py"], cwd=work,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "tail": ["solve timed out"]}
    out = (r.stdout or "") + (r.stderr or "")
    blocked = ("ModuleNotFoundError" in out) or ("ImportError" in out)
    return {
        "ok": r.returncode == 0,
        "returncode": r.returncode,
        "tail": out.strip().splitlines()[-15:] if out.strip() else [],
        "env_blocked": blocked,
    }


def grade(work: Path) -> dict:
    """Run the official grader. Returns a dict with success / max_abs_error /
    keys_match / detail / env_blocked / method."""
    # 1) subprocess run: some eval scripts (e.g. #92) print a JSON payload.
    proc = subprocess.run(
        [sys.executable, "run_eval.py"], cwd=work,
        capture_output=True, text=True, timeout=600,
    )
    out, err = (proc.stdout or ""), (proc.stderr or "")
    ej = None
    try:
        ej = json.loads(out)
    except Exception:
        ej = None
    if isinstance(ej, dict) and ("success" in ej or "max_abs_error" in ej):
        mae = ej.get("max_abs_error")
        keys = ej.get("keys_match")
        success = bool(ej.get("success", (float(mae) <= TOLERANCE) if mae is not None else False))
        return {
            "success": success,
            "max_abs_error": float(mae) if mae is not None else None,
            "keys_match": float(keys) if isinstance(keys, (int, float)) else None,
            "detail": out.strip()[-300:],
            "env_blocked": False,
            "method": "json",
        }

    # 2) authoritative import-based grade (mirrors materialize_and_eval.grade).
    if "ModuleNotFoundError" in err or "ImportError" in err:
        miss = re.findall(r"ModuleNotFoundError: No module named '([^']+)'", err)
        miss += re.findall(r"ImportError: No module named '([^']+)'", err)
        return {
            "success": False, "max_abs_error": None, "keys_match": None,
            "detail": err.strip()[-300:],
            "env_blocked": True,
            "missing": sorted(set(miss)),
            "method": "subprocess_import_error",
        }
    spec = importlib.util.spec_from_file_location("run_eval_mod", work / "run_eval.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(work))
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 - any import failure = env blocker
        miss = re.findall(r"No module named '([^']+)'", str(e))
        return {
            "success": False, "max_abs_error": None, "keys_match": None,
            "detail": f"eval_import:{type(e).__name__}:{e}",
            "env_blocked": isinstance(e, (ImportError, ModuleNotFoundError)),
            "missing": sorted(set(miss)),
            "method": "import_error",
        }
    cwd0 = os.getcwd()
    os.chdir(work)
    try:
        if hasattr(mod, "eval"):
            res = mod.eval()
            if isinstance(res, tuple):
                success = bool(int(res[0]) == 1)
                detail = str(res[1]) if len(res) > 1 else ""
            else:
                success = bool(res)
                detail = ""
            return {"success": success, "max_abs_error": None, "keys_match": None,
                    "detail": detail[:300], "env_blocked": False, "method": "eval"}
        if hasattr(mod, "main"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
            return {"success": bool(rc == 0), "max_abs_error": None, "keys_match": None,
                    "detail": buf.getvalue().strip()[-300:], "env_blocked": False, "method": "main"}
        return {"success": False, "max_abs_error": None, "keys_match": None,
                "detail": "eval script exposes neither eval() nor main()",
                "env_blocked": False, "method": "none"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "max_abs_error": None, "keys_match": None,
                "detail": f"eval_run:{type(e).__name__}:{e}", "env_blocked": False,
                "method": "run_error"}
    finally:
        os.chdir(cwd0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="SAB task id, e.g. sab.hrv_analyze_34")
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    task_id = args.task
    notes: list[str] = []
    try:
        meta = resolve_task(task_id)
    except KeyError as e:
        notes.append(str(e))
        meta = {"iid": 0, "gp": "", "es": "", "out": "", "domain": "", "deps": []}

    data_dir = args.data_dir
    if not os.path.exists(data_dir):
        data_dir = os.environ.get("AGENT_DATA_DIR", data_dir)
    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    work = Path(_SCRATCH) / f"sab_{meta['iid'] if meta['iid'] else 'x'}_{task_id.split('_')[-1]}"
    work.mkdir(parents=True, exist_ok=True)

    ok = materialize(task_id, meta, work, notes) if meta.get("gp") else False
    if not ok:
        # cannot even stage the task; emit an honest result and bail.
        result = _build_result(task_id, success=False, primary=1.0, max_abs_error=1.0,
                               keys_match=0.0, detail="; ".join(notes) or "staging failed",
                               isolation=isolation, notes=notes + ["staging failed"],
                               env_blocked=True, missing=meta.get("deps", []))
        _write(result, args.result_name)
        return 0

    solve = run_solve(work)
    if not solve["ok"]:
        if solve.get("env_blocked"):
            notes.append("solve.py import failed (missing dependency)")
        else:
            notes.append("solve.py failed: " + " | ".join(solve.get("tail", [])[-3:]))

    g = grade(work)
    if g.get("env_blocked"):
        notes.append("env-blocked: missing " + ", ".join(g.get("missing", [])) or "dependency")
    if g.get("detail"):
        notes.append("eval: " + g["detail"][:200])

    env_blocked = bool(g.get("env_blocked")) or (not solve["ok"] and solve.get("env_blocked"))

    # real numeric error if the grader emitted one, else 0.0 on success / 1.0 on failure
    mae = g.get("max_abs_error")
    success = bool(g.get("success"))
    if mae is not None:
        primary = float(mae)
    else:
        primary = 0.0 if success else 1.0
    keys_match = g.get("keys_match")
    if keys_match is None:
        keys_match = 1.0 if success else 0.0

    missing = g.get("missing") or meta.get("deps", [])
    # drop stdlib false-positives (e.g. zipfile) from the reported missing set
    _STDLIB = {
        "os", "sys", "re", "json", "math", "time", "warnings", "typing",
        "collections", "pathlib", "random", "statistics", "functools",
        "itertools", "datetime", "glob", "shutil", "traceback", "pickle",
        "copy", "abc", "enum", "dataclasses", "contextlib", "inspect",
        "hashlib", "base64", "string", "textwrap", "logging", "argparse",
        "csv", "io", "numbers", "fractions", "decimal", "heapq", "subprocess",
        "zipfile", "__future__",
    }
    missing = sorted({m for m in missing if m and m not in _STDLIB})
    result = _build_result(
        task_id, success=success, primary=primary,
        max_abs_error=(float(mae) if mae is not None else primary),
        keys_match=float(keys_match), detail=g.get("detail", ""),
        isolation=isolation, notes=notes, env_blocked=env_blocked, missing=missing,
    )
    _write(result, args.result_name)
    return 0


def _build_result(task_id, success, primary, max_abs_error, keys_match, detail,
                  isolation, notes, env_blocked, missing):
    passed = bool(success) and (primary <= TOLERANCE)
    return {
        "task_id": task_id,
        "eval_metric": "max_abs_error",
        "threshold": TOLERANCE,
        "op": "le",
        "passed": bool(passed),
        "gate_passed": bool(passed),
        # metrics MUST be numeric-only: the upstream EvalCompletedEvent validates
        # each value as a float. Keep the four contract fields here; put any
        # human-readable diagnostics in the top-level "diagnostics" block.
        "metrics": {
            "primary": float(primary),
            "max_abs_error": float(max_abs_error),
            "success": 1.0 if success else 0.0,
            "keys_match": float(keys_match),
        },
        "diagnostics": {
            "sab_detail": (detail or "")[:500],
            "env_blocked": bool(env_blocked),
            "missing_deps": list(missing),
        },
        "isolation": isolation,
        "port_notes": notes[-8:],
        "report_ref": f"oss://{task_id}",
    }


def _write(result, result_name):
    Path(_SCRATCH).mkdir(parents=True, exist_ok=True)
    out_path = os.path.join(_SCRATCH, result_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
