"""Batch runner for ScienceAgentBench lightweight CSV/JSON tasks.

Pipeline per task (when its curated data dir is present):

    codex  ->  solve.py  ->  run_eval.py  ->  pass/fail (+ metrics)

* codex is the local agent standing in for the original GPT-4o agent. Its config
  already routes through the local tmeoa proxy, so the *agent* substitution is
  realised here.
* run_eval.py executes the generated program and grades its output against the
  gold reference — no vision judge needed for CSV/JSON tasks.

Tasks whose curated data dir is absent are reported ``skipped`` because the full
SAB benchmark (datasets + gold + eval) is behind a password-gated OSU SharePoint
zip; the annotation sheet alone (see sab_task_index.json) yields the task list and
classification but not the runnable corpus.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SAB = Path(__file__).resolve().parent
CODEX = "/opt/homebrew/bin/codex"
INDEX = SAB / "sab_task_index.json"


def find_task_dir(iid: int) -> Path | None:
    # SAB instance ids are 1-102 but curated dirs are zero-padded to 2 digits
    # (e.g. task_05_dkpes, task_92_h_importances). Try both forms.
    for pat in (f"task_{iid}_*", f"task_{iid:02d}_*"):
        matches = sorted(SAB.glob(pat))
        if matches:
            return matches[0]
    return None


def run_eval(tdir: Path, timeout: int = 300) -> dict:
    try:
        r = subprocess.run(
            [sys.executable, "run_eval.py"],
            cwd=tdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"stage": "eval", "success": False, "error": "eval timed out"}
    success = r.returncode == 0
    snippet = (r.stdout or r.stderr).strip().splitlines()
    return {
        "stage": "eval",
        "success": success,
        "returncode": r.returncode,
        "output_tail": snippet[-6:] if snippet else [],
    }


def run_codex(tdir: Path, timeout: int = 600) -> dict:
    """Generate solve.py with codex (non-interactive) and return a status dict."""
    prompt = (
        "Read task_inst.md in this directory and write a self-contained, "
        "dependency-light Python program named solve.py that produces the exact "
        "pred_results/ output file the task asks for. Then run `python solve.py` "
        "to generate it. Do not modify run_eval.py or the gold files."
    )
    backup = tdir / "solve.py.bak"
    if (tdir / "solve.py").exists():
        shutil.copy(tdir / "solve.py", backup)
    try:
        r = subprocess.run(
            [
                CODEX,
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "-c",
                "shell_environment_policy.inherit=all",
                prompt,
            ],
            cwd=tdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stage": "codex_gen",
            "returncode": r.returncode,
            "stdout_tail": r.stdout.strip().splitlines()[-8:],
            "stderr_tail": r.stderr.strip().splitlines()[-8:],
        }
    except subprocess.TimeoutExpired:
        return {"stage": "codex_gen", "error": "codex timed out"}
    finally:
        if backup.exists():
            backup.unlink()


def process(iid: int, do_codex: bool, timeout: int) -> dict:
    tdir = find_task_dir(iid)
    rec: dict = {"instance_id": iid}
    if tdir is None:
        rec.update(
            status="skipped",
            reason="curated data dir missing (gated SAB benchmark not available)",
        )
        return rec
    rec["task_dir"] = tdir.name
    if do_codex:
        rec["codex"] = run_codex(tdir, timeout=timeout)
    rec["eval"] = run_eval(tdir, timeout=timeout)
    rec["status"] = "pass" if rec["eval"].get("success") else "fail"
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ids",
        default="remaining",
        help="'remaining' (default), 'validated', 'all', or comma-separated ids",
    )
    ap.add_argument("--codex", action="store_true", help="also run codex to (re)generate solve.py")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    index = json.loads(INDEX.read_text())
    by_id = {t["instance_id"]: t for t in index["tasks"]}

    if args.ids == "remaining":
        ids = index["remaining_light_csv_json"]
    elif args.ids == "validated":
        ids = index["already_validated"]
    elif args.ids == "all":
        ids = [t["instance_id"] for t in index["tasks"]]
    else:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]

    records = [process(i, args.codex, args.timeout) for i in ids]

    n_pass = sum(1 for r in records if r.get("status") == "pass")
    n_skip = sum(1 for r in records if r.get("status") == "skipped")
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "codex+eval" if args.codex else "eval-only",
        "requested_ids": ids,
        "summary": {
            "n": len(records),
            "pass": n_pass,
            "fail": sum(1 for r in records if r.get("status") == "fail"),
            "skipped": n_skip,
        },
        "records": records,
    }
    out_path = SAB / "batch_sab_results.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"mode={out['mode']} pass={n_pass} fail={out['summary']['fail']} skipped={n_skip} n={len(records)}")
    for r in records:
        print(f"  #{r['instance_id']:>3} {r['status']:8s} {r.get('reason', r.get('task_dir',''))}")


if __name__ == "__main__":
    main()
