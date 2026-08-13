"""Local evaluator for SAB instance #92 (JNMF importance factors).

Compares the agent-generated `pred_results/jnmf_h_importances.json` against the gold
reference within tolerance 1e-4, mirroring the official eval_h_importances.py logic.
"""
import json
import sys


def main() -> int:
    pred_path = "pred_results/jnmf_h_importances.json"
    gold_path = "gold/jnmf_h_importances_gold.json"
    thres = 1e-4

    try:
        with open(gold_path) as f:
            gold = json.load(f)
        with open(pred_path) as f:
            pred = json.load(f)
    except FileNotFoundError as e:
        print(json.dumps({"success": False, "error": f"missing file: {e}"}, indent=2))
        return 1

    keys_ok = set(pred.keys()) == set(gold.keys())
    max_err = 0.0
    per_key = {}
    for key in gold:
        g = float(gold[key])
        p = float(pred.get(key, float("nan")))
        err = abs(g - p)
        per_key[key] = {"gold": g, "pred": p, "abs_err": err}
        max_err = max(max_err, err)

    success = keys_ok and max_err <= thres
    result = {
        "success": bool(success),
        "keys_match": bool(keys_ok),
        "max_abs_error": max_err,
        "tolerance": thres,
        "per_key": per_key,
    }
    print(json.dumps(result, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
