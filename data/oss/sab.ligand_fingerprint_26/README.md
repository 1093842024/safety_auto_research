# sab.ligand_fingerprint_26  (SAB instance #26)

ScienceAgentBench lightweight agent-eval task - Computational Chemistry.

This directory is a CONTRACT SENTINEL only. The real dataset and the
official gold solve.py / run_eval.py live read-only in the SAB vendor
corpus and are symlinked at launch time by the sandbox runner:

    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/datasets/            (input data)
    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/gold_programs/ligand_fingerprint.py   (reference solve program)
    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/eval_programs/eval_fingerprint.py   (grader)

L1 launch:

    python /repo/scripts/sandbox_examples/run_sab_sandbox.py --data-dir /data/ --task sab.ligand_fingerprint_26

External dependency NOT in the numpy/pandas/scikit-learn/scipy sandbox image:
    MDAnalysis / prolif

If that dependency is absent the launch still completes honestly with
passed=False plus a port_notes entry naming the missing module.
