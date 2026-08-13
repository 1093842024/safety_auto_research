#!/usr/bin/env python3
"""L1 verification harness for Tier A' (torch custom tasks) + Tier B (SAB 19).

Runs each task's runner inside the full sandbox image (safety-research-sandbox:full)
with the executor's mount scheme (--network none, /data ro, /repo ro, /scratch rw)
and records the real numeric primary metric + passed flag.

Usage:
  python scripts/_verify_tierA_B.py --image safety-research-sandbox:full \
      --out experiments/oss_validation/verify_tierA_B_results.json
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys

WS = "/Users/glennge/work/github/AI_research"
REPO = os.path.join(WS, "safety_auto_research")
SCRATCH = "/tmp/agent_sb_scratch"

SAB_IDS = [
    "sab.bio_eventrelated_analyze_29", "sab.cft_37", "sab.cogsci_pattern_high_sim_67",
    "sab.deforestation_21", "sab.dili_models_ecfp_rf_18", "sab.dili_models_ecfp_svm_19",
    "sab.hrv_analyze_34", "sab.imu_44", "sab.ligand_fingerprint_26", "sab.mat_feature_select_2",
    "sab.md_knn_41", "sab.md_rf_40", "sab.nvc_accuracies_60", "sab.nvc_gen_ind_58",
    "sab.polynomial_fit_87", "sab.predict_bulk_modulus_3", "sab.questionnaire_45",
    "sab.rrv_analyze_35", "sab.saliva_85",
]


def run_docker(img, cmd, data_mount):
    """data_mount: host path mounted read-only at /data."""
    full = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{data_mount}:/data:ro",
        "-v", f"{REPO}:/repo:ro",
        "-v", f"{SCRATCH}:/scratch:rw",
        "-e", "AGENT_DATA_DIR=/data", "-e", "AGENT_SCRATCH_DIR=/scratch",
        img,
    ] + cmd
    try:
        out = subprocess.run(full, capture_output=True, text=True, timeout=600)
        return out.returncode, out.stdout, out.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def parse_result(stdout):
    # Extract the outermost JSON object from stdout (the runner prints a single
    # json.dumps(result) block; using rfind('{') is wrong because it lands on
    # the innermost nested dict and leaves dangling closing braces).
    s = (stdout or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < 0 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception:
        return None


def verify_sab(img):
    results = []
    for tid in SAB_IDS:
        rc, so, se = run_docker(img, [
            "python", "/repo/scripts/sandbox_examples/run_sab_sandbox.py",
            "--data-dir", "/data/", "--task", tid,
        ], os.path.join(REPO, "data", "oss"))
        res = parse_result(so) or {}
        results.append({
            "task_id": tid, "group": "sab",
            "returncode": rc,
            "primary": res.get("metrics", {}).get("primary"),
            "passed": res.get("passed"),
            "eval_metric": res.get("eval_metric"),
            "isolation": res.get("isolation"),
            "notes": res.get("port_notes"),
            "stderr_tail": (se or "")[-300:],
        })
    return results


def verify_custom_torch(img):
    results = []
    # image classification
    rc, so, se = run_docker(img, [
        "python", "/repo/scripts/sandbox_examples/run_image_cls_sandbox.py",
        "--data-dir", "/data/image_cls_demo", "--arch", "tiny_cnn",
        "--epochs", "6", "--eval-metric", "top1_accuracy", "--threshold", "0.5",
    ], os.path.join(REPO, "benchmark_tasks", "sample_data"))
    res = parse_result(so) or {}
    results.append({
        "task_id": "custom.4_2", "group": "custom_torch",
        "returncode": rc, "primary": res.get("metrics", {}).get("primary"),
        "passed": res.get("passed"), "eval_metric": res.get("eval_metric"),
        "isolation": res.get("isolation"), "notes": res.get("port_notes"),
        "stderr_tail": (se or "")[-300:],
    })
    # audio classification
    rc, so, se = run_docker(img, [
        "python", "/repo/scripts/sandbox_examples/run_audio_cls_sandbox.py",
        "--manifest", "/data/audio_cls_demo/manifest.csv",
        "--data-dir", "/data/audio_cls_demo", "--epochs", "8",
        "--eval-metric", "accuracy", "--threshold", "0.25",
    ], os.path.join(REPO, "benchmark_tasks", "sample_data"))
    res = parse_result(so) or {}
    results.append({
        "task_id": "custom.4_3", "group": "custom_torch",
        "returncode": rc, "primary": res.get("metrics", {}).get("primary"),
        "passed": res.get("passed"), "eval_metric": res.get("eval_metric"),
        "isolation": res.get("isolation"), "notes": res.get("port_notes"),
        "stderr_tail": (se or "")[-300:],
    })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="safety-research-sandbox:full")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(SCRATCH, exist_ok=True)
    out = {
        "image": args.image,
        "sab": verify_sab(args.image),
        "custom_torch": verify_custom_torch(args.image),
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    # summary
    allr = out["sab"] + out["custom_torch"]
    ok = [r for r in allr if r["passed"]]
    print(f"TOTAL={len(allr)} passed={len(ok)}")
    for r in allr:
        print(f"  {r['task_id']:34s} primary={r['primary']} passed={r['passed']} rc={r['returncode']}")
    # failures detail
    for r in allr:
        if not r["passed"]:
            print(f"  [FAIL] {r['task_id']}: rc={r['returncode']} notes={r['notes']} err={r['stderr_tail']}")


if __name__ == "__main__":
    main()
