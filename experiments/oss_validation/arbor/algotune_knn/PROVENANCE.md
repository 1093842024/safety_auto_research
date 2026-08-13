# Provenance — algotune_knn

## Source

Modelled on [AlgoTune](https://algotune.io/) (NeurIPS 2025), a benchmark of
single-editable-solver optimization tasks. This is **not** a verbatim copy of any AlgoTune
task — it's an independent re-implementation of the same *structure* (one editable solver +
a fixed problem generator, reference solver, and correctness verifier) for a brute-force
k-nearest-neighbours task. The data is generated deterministically from integer seeds via
`numpy.random.default_rng(seed)`; nothing is downloaded or scraped.

## Setup & environment

- **Hardware:** CPU only — no GPU.
- **Python:** ≥ 3.10. **Install:** `pip install -r requirements.txt` (just NumPy).
- **Offline:** yes — no network, no services.
- **License:** MIT (code and the synthetic, seed-generated data); freely redistributable.
- **Threads:** `eval.sh` pins `OMP_NUM_THREADS=1` (and BLAS equivalents) so the measured
  time reflects the algorithm, not the core count — do not remove it.

## Baseline

The shipped `solution.py` is the **naive brute-force baseline**, identical to
`task.reference_solver`: full pairwise distances by broadcasting, then `argsort` and take the
first `k`. Correct but unoptimised, so its speedup over the reference is **~1.0x** by
construction — that's the number to beat. The headroom (partial selection via `argpartition`,
the `|x−y|² = |x|² − 2x·y + |y|²` GEMM expansion, dtype/blocking) is what makes the search
interesting.

**Results vary:** the score is a ratio of medians, so it wobbles a couple of percent
run-to-run and depends on the machine — the *ratio* is stable under single-thread pinning,
the absolute speedup is not.

## Contamination assessment

No held-out leakage and no pre-training contamination. Dev seeds `1000..1002` and test seeds
`9000..9002` are disjoint ranges (see `DEV_SEED_BASE` / `TEST_SEED_BASE` in `eval.py`), so a
solution tuned on dev has never seen the test instances. The instances are random point
clouds generated from seeds — not text or images from any corpus — so there is nothing a
model could have memorised, and the metric is wall-clock speedup, not recall of an answer
(`is_solution` recomputes the ground truth, so "fast but wrong" scores 0).

## Caveats

- **Timing metric is hardware-dependent** — compare the ratio, not the absolute number, and
  keep the single-thread pinning.
- **Not bit-exact** — two runs differ by a few percent; that's timing noise, not a bug.
- **Seed bases live in `eval.py`** — if you change `DEV_SEED_BASE` / `TEST_SEED_BASE`, keep
  dev and test disjoint.
