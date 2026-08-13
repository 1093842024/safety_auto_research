# algotune_knn

An AlgoTune-style mini benchmark: make a brute-force **k-nearest-neighbours** computation
*faster* without changing what it computes. CPU-only, no API key, deterministic, sub-second.

## The task

Given a database of points and a batch of queries, return each query's **k nearest
neighbours** (Euclidean distance). The reference implementation is intentionally naive —
full pairwise distances followed by a full sort. The goal is to compute the **same**
neighbours **faster**.

## Metric

`bash eval.sh dev|test` prints one `score:` line — the speedup
`median(reference_time) / median(solution_time)`, measured after a correctness gate.
**Higher is better.** The shipped `solution.py` equals the reference, so the baseline is
about **1.0x**; a solution that fails the correctness check scores **0.0**.

## What Arbor may edit

`solution.py` (the `solve(problem)` function) is the editable baseline. The protected
harness — `task.py` (problem generator, reference solver, independent verifier), `eval.py`,
and `eval.sh` — must not change.

## Dev / test

Dev and test use **disjoint seed ranges** (`1000+` vs `9000+`), so the signal you iterate
on is never the data you're finally judged on:

```bash
bash eval.sh dev     # iterate here
bash eval.sh test    # held-out gate
```

## Optimize with Arbor

Copy this folder out of the Arbor checkout (it uses git worktrees), then run `arbor`:

```bash
cp -r arbor-zoo/algotune_knn /tmp/algotune_knn
cd /tmp/algotune_knn && git init -q && git add -A && git commit -qm baseline
arbor
```

There's real headroom — `argpartition` partial selection, the
`|x−y|² = |x|² − 2x·y + |y|²` GEMM distance expansion, dtype and blocking tweaks — so the
search has several genuine branches, not one trick.

See [`PROVENANCE.md`](PROVENANCE.md) for source, setup, and how the baseline works.
