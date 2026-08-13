# sab.bio_eventrelated_analyze_29  (SAB instance #29)

ScienceAgentBench lightweight agent-eval task - Psychology and Cognitive science.

This directory is a CONTRACT SENTINEL only. The real dataset and the
official gold solve.py / run_eval.py live read-only in the SAB vendor
corpus and are symlinked at launch time by the sandbox runner:

    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/datasets/            (input data)
    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/gold_programs/bio_eventrelated_analyze.py   (reference solve program)
    benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/eval_programs/eval_bio_eventrelated_analyze.py   (grader)

L1 launch:

    python /repo/scripts/sandbox_examples/run_sab_sandbox.py --data-dir /data/ --task sab.bio_eventrelated_analyze_29

External dependency NOT in the numpy/pandas/scikit-learn/scipy sandbox image:
    neurokit2

If that dependency is absent the launch still completes honestly with
passed=False plus a port_notes entry naming the missing module.
