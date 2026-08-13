"""Materialize SAB lightweight CSV/JSON task dirs from the LOCAL corpus and grade them.

Pipeline per task (infrastructure / runnability check):
    gold_program (reference solve)  ->  solve.py  ->  run_eval.py  ->  pass/fail

The gold program is the official reference solution; running it must reproduce the
precomputed gold output, so a PASS proves (a) the dataset is present, (b) the eval /
grading logic is correct, and (c) the required dependencies are satisfiable in this
environment. An ImportError / env failure is reported as ``env_blocked`` (NOT a
substitution defect) with the missing package named.

Gold/pred file names are scraped from the official eval script (they are NOT a uniform
function of the output_fname), and full dataset subtrees are copied so that package
imports like ``benchmark.datasets.nvc.scripts...`` resolve.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAB = Path(
    "/Users/glennge/work/github/AI_research/safety_auto_research/benchmark_tasks/"
    "suites/data/vendor/ScienceAgentBench/benchmark"
)
INDEX = ROOT / "sab_task_index.json"


def load_index() -> dict:
    return json.loads(INDEX.read_text())


def extract_gold_pred(es_src: str):
    """(gold_basename, pred_basename) as referenced inside the eval script."""
    gm = re.search(r"gold_results/([^\"'\s]+)", es_src)
    pm = re.search(r"pred_results/([^\"'\s]+)", es_src)
    return (gm.group(1) if gm else None), (pm.group(1) if pm else None)


def extract_dataset_roots(gp_src: str):
    roots = set()
    for m in re.finditer(r"benchmark/datasets/([A-Za-z0-9_\-\.]+)", gp_src):
        roots.add(m.group(1))
    for m in re.finditer(r"benchmark\.datasets\.([A-Za-z0-9_]+)", gp_src):
        roots.add(m.group(1))
    return roots


def materialize(iid: int, t: dict, force: bool = False) -> Path:
    gp = t["gold_program_name"]
    es = t["eval_script_name"]
    of = t["output_fname"]
    gp_src = (SAB / "gold_programs" / gp).read_text()
    slug = gp[:-3]
    tdir = ROOT / f"task_{iid:02d}_{slug}"
    if tdir.exists() and not force:
        return tdir  # already materialized (keep any existing solve.py / custom eval)
    tdir.mkdir(parents=True, exist_ok=True)

    # --- datasets (full subtree per referenced root) ---
    for ds in extract_dataset_roots(gp_src):
        src = SAB / "datasets" / ds
        if not src.exists():
            print(f"  [WARN] #{iid} dataset root missing: {ds}")
            continue
        dst = tdir / "benchmark" / "datasets" / ds
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    # make benchmark a package so `from benchmark.datasets...` imports resolve
    for p in (tdir / "benchmark" / "__init__.py", tdir / "benchmark" / "datasets" / "__init__.py"):
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("")

    # --- gold output (exact name scraped from eval script) ---
    es_src = (SAB / "eval_programs" / es).read_text()
    gold_name, pred_name = extract_gold_pred(es_src)
    (tdir / "gold").mkdir(exist_ok=True)
    if gold_name:
        gsrc = SAB / "eval_programs" / "gold_results" / gold_name
        if gsrc.exists():
            shutil.copy(gsrc, tdir / "gold" / gold_name)
        else:
            print(f"  [WARN] #{iid} gold output missing: {gold_name}")
    else:
        print(f"  [WARN] #{iid} could not scrape gold path from {es}")

    # reference gold program
    shutil.copy(SAB / "gold_programs" / gp, tdir / "gold" / gp)

    # run_eval.py : patch official eval's gold path to local gold/
    patched = re.sub(r"(?:\./)?benchmark/eval_programs/gold_results/", "gold/", es_src)
    (tdir / "run_eval.py").write_text(patched)

    # task_inst.md
    (tdir / "task_inst.md").write_text(gen_inst(t, pred_name or of))
    return tdir


def gen_inst(t: dict, pred_name: str) -> str:
    return (
        f"# ScienceAgentBench · instance #{t['instance_id']} — {t['gold_program_name']}\n\n"
        f"**Domain:** {t.get('domain','')}\n"
        f"**Subtask categories:** {t.get('subtask_categories','')}\n"
        f"**Target output:** a self-contained Python program that writes `{pred_name}`\n\n"
        "## Task\n"
        f"See the official SAB task specification for instance #{t['instance_id']}.\n"
        "Datasets live under `benchmark/datasets/`. The gold reference program is "
        f"`gold/{t['gold_program_name']}`.\n"
        "Produce the required prediction file; grading runs `run_eval.py`.\n"
    )


def run_solve(tdir: Path, gp: str, timeout: int = 300, python: str | None = None) -> dict:
    """Run the gold program as solve.py from cwd=tdir (script dir = tdir => package imports resolve).

    JOBLIB_MULTIPROCESSING=0 forces joblib's sequential backend, which avoids the
    ``PicklingError: Could not pickle the task to send it to the workers`` raised by
    reference gold programs that Parallel() a locally-defined function under joblib
    1.5.x + CPython 3.13. The computation is unchanged, only serialized.
    """
    (tdir / "pred_results").mkdir(parents=True, exist_ok=True)
    solve = tdir / "solve.py"
    if solve.exists():
        solve.unlink()
    shutil.copy(tdir / "gold" / gp, solve)
    env = dict(os.environ)
    env["JOBLIB_MULTIPROCESSING"] = "0"
    try:
        r = subprocess.run(
            [python or sys.executable, "solve.py"],
            cwd=tdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"stage": "solve", "ok": False, "error": "solve timed out"}
    out = (r.stdout or "") + (r.stderr or "")
    return {
        "stage": "solve",
        "ok": r.returncode == 0,
        "returncode": r.returncode,
        "tail": out.strip().splitlines()[-12:] if out.strip() else [],
    }


def grade(tdir: Path) -> dict:
    spec = importlib.util.spec_from_file_location("run_eval_mod", tdir / "run_eval.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # ImportError for missing dep, etc.
        return {"success": False, "error": f"eval_import:{type(e).__name__}:{e}"}
    cwd0 = os.getcwd()
    os.chdir(tdir)  # eval scripts use cwd-relative pred_results/ and gold/ paths
    try:
        if hasattr(mod, "eval"):
            res = mod.eval()
            return {"success": bool(int(res[0]) == 1), "detail": str(res[1]) if len(res) > 1 else ""}
        # custom evaluator with main() returning int + printing
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        return {"success": bool(rc == 0), "detail": buf.getvalue().strip()[-200:]}
    except Exception as e:
        return {"success": False, "error": f"eval_run:{type(e).__name__}:{e}"}
    finally:
        os.chdir(cwd0)


def process(iid: int, t: dict, force: bool = False) -> dict:
    rec: dict = {"instance_id": iid}
    tdir = materialize(iid, t, force=force)
    rec["task_dir"] = tdir.name
    solve = run_solve(tdir, t["gold_program_name"])
    rec["solve"] = {"ok": solve["ok"], "returncode": solve.get("returncode")}
    if not solve["ok"]:
        rec["status"] = "env_blocked" if "ImportError" in "\n".join(solve.get("tail", [])) else "solve_failed"
        rec["solve_tail"] = solve.get("tail", [])
        return rec
    g = grade(tdir)
    rec["grade"] = g
    rec["status"] = "pass" if g.get("success") else "fail"
    return rec


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="remaining")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--python", default=None, help="Python interpreter to run solve.py with (e.g. a numpy<2 venv)")
    args = ap.parse_args()

    idx = load_index()
    by_id = {t["instance_id"]: t for t in idx["tasks"]}
    if args.ids == "remaining":
        ids = idx["remaining_light_csv_json"]
    elif args.ids == "all":
        ids = [t["instance_id"] for t in idx["tasks"] if t["category"] == "light_csv_json"]
    else:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]

    records = [process(i, by_id[i], force=args.force) for i in ids]
    # re-run with explicit interpreter if provided (process does not thread python through solve)
    if args.python:
        for r in records:
            tdir = ROOT / r.get("task_dir", "")
            sol = run_solve(tdir, by_id[r["instance_id"]]["gold_program_name"], timeout=args.timeout, python=args.python)
            r["solve"] = {"ok": sol["ok"], "returncode": sol.get("returncode"), "interpreter": args.python}
            if not sol["ok"]:
                r["status"] = "env_blocked" if "ImportError" in "\n".join(sol.get("tail", [])) else "solve_failed"
                r["solve_tail"] = sol.get("tail", [])
                continue
            g = grade(tdir)
            r["grade"] = g
            r["status"] = "pass" if g.get("success") else "fail"
    # Merge into existing results JSON keyed by instance_id (incremental accumulation).
    out_path = ROOT / "batch_sab_gold_eval.json"
    merged = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            for r in prev.get("records", []):
                merged[r["instance_id"]] = r
        except Exception:
            merged = {}
    for r in records:
        merged[r["instance_id"]] = r
    all_records = [merged[k] for k in sorted(merged)]
    n_pass = sum(1 for r in all_records if r.get("status") == "pass")
    n_fail = sum(1 for r in all_records if r.get("status") == "fail")
    n_block = sum(1 for r in all_records if r.get("status") in ("env_blocked", "solve_failed"))
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "gold-as-solve (reference program) -> run_eval.py",
        "requested_ids": ids,
        "summary": {"n_total": len(all_records), "pass": n_pass, "fail": n_fail, "blocked": n_block},
        "records": all_records,
    }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"pass={n_pass} fail={n_fail} blocked={n_block} n_total={len(all_records)}")
    for r in all_records:
        print(f"  #{r['instance_id']:>3} {r['status']:11s} {r.get('task_dir','')}  {r.get('grade',{}).get('detail','')}")


if __name__ == "__main__":
    main()
