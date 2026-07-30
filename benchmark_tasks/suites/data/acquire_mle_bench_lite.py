"""
Batch download + prepare of MLE-bench "lite" competitions whose prepared
dataset size is < 100 MB, using the vendored mle-bench package.

Scope (see mle_bench_competitions.json):
  - in_lite == True  (the canonical low-complexity / "lite" split)
  - dataset_size_gb < 0.1  (i.e. < 100 MB)
  - excluded != True        (skip known-issue / label-leakage competitions)

For each target competition this script:
  1. downloads the raw Kaggle zip via the Kaggle API,
  2. verifies checksums (retry with skip_verification on mismatch),
  3. runs the competition's prepare_fn to build public/ + private/.
Failures (e.g. competition rules not yet accepted on Kaggle) are recorded
with a URL so they can be retried after accepting rules in a browser.
"""
import json
import sys
import traceback
from pathlib import Path

REPO = Path("/Users/glennge/work/github/AI_research/safety_auto_research")
VENDOR = REPO / "benchmark_tasks/suites/data/vendor/mle-bench"
DATA_DIR = REPO / "benchmark_tasks/suites/data/mle_bench_data"
MANIFEST = REPO / "benchmark_tasks/suites/data/mle_bench_competitions.json"

sys.path.insert(0, str(VENDOR))

from mlebench.registry import Registry
from mlebench.data import download_and_prepare_dataset


def select_targets(manifest_path: Path, size_gb_limit: float = 0.1):
    import yaml  # pyyaml
    comps = yaml.safe_load(manifest_path.read_text())
    targets = []
    for c in comps:
        if c.get("excluded"):
            continue
        if not c.get("in_lite"):
            continue
        size = c.get("dataset_size_gb")
        if size is None or size >= size_gb_limit:
            continue
        targets.append(c["competition_id"])
    return targets


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    targets = select_targets(MANIFEST)
    print(f"[info] selected {len(targets)} targets (lite & <100MB): {targets}", flush=True)

    reg = Registry(DATA_DIR)
    results = []
    for cid in targets:
        entry = {"id": cid, "status": None, "detail": None,
                 "needs_rule_acceptance": False, "rules_url": None,
                 "public": None, "private": None}
        try:
            comp = reg.get_competition(cid)
            try:
                download_and_prepare_dataset(comp, keep_raw=True)
            except Exception as e1:  # retry without checksum verification
                if "checksum" in str(e1).lower():
                    print(f"[warn] {cid}: checksum mismatch, retrying with skip_verification", flush=True)
                    download_and_prepare_dataset(comp, keep_raw=True, skip_verification=True)
                else:
                    raise
            entry["status"] = "prepared"
            entry["public"] = str(comp.public_dir)
            entry["private"] = str(comp.private_dir)
            # report sizes
            raw_size = sum(f.stat().st_size for f in comp.raw_dir.rglob("*") if f.is_file())
            entry["raw_bytes"] = raw_size
        except Exception as e:
            entry["status"] = "failed"
            msg = f"{type(e).__name__}: {str(e)[:400]}"
            entry["detail"] = msg
            low = msg.lower()
            # The vendored mle-bench prompts `input()` to accept rules; under a
            # non-interactive run this raises EOFError. That is the signal that the
            # competition rules must be accepted on the Kaggle website first.
            if isinstance(e, EOFError) or "accept" in low or "rules" in low or "must accept" in low:
                entry["needs_rule_acceptance"] = True
                entry["rules_url"] = f"https://www.kaggle.com/c/{cid}/rules"
                entry["detail"] = "Kaggle competition rules not yet accepted. " + msg
            print(f"[fail] {cid}: {msg}", flush=True)
        else:
            print(f"[ok]   {cid}: prepared", flush=True)
        results.append(entry)

    out = REPO / "benchmark_tasks/suites/data/mle_bench_acquire_report.json"
    out.write_text(json.dumps(results, indent=2))
    print("\n=== SUMMARY ===", flush=True)
    for r in results:
        flag = "  (NEEDS RULE ACCEPTANCE)" if r["needs_rule_acceptance"] else ""
        print(f"  {r['id']:<55} {r['status']}{flag}", flush=True)
    print(f"\nreport -> {out}", flush=True)


if __name__ == "__main__":
    main()
