#!/usr/bin/env bash
# Single-core, low-noise evaluation harness for the k-NN AlgoTune-style task.
#
# Usage:
#   bash eval.sh            # dev split  (Arbor iterates on this)
#   bash eval.sh dev
#   bash eval.sh test       # held-out split (used to gate merges)
#
# Prints a "score: <speedup>" line that Arbor reads as the metric (maximize).
set -euo pipefail

SPLIT="${1:-dev}"

# Pin BLAS / OpenMP to a single thread so the measured time reflects the
# *algorithm*, not how many cores the machine has. This is what keeps speedups
# meaningful and comparable from run to run.
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export PYTHONHASHSEED=0

HERE="$(cd "$(dirname "$0")" && pwd)"

# Pick an interpreter: explicit $PYTHON, else python3, else python.
if [ -n "${PYTHON:-}" ]; then
  :
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

# Bind to a single CPU when taskset is available (Linux). Harmless to skip on
# macOS, where taskset does not exist.
if command -v taskset >/dev/null 2>&1; then
  exec taskset -c 0 "$PYTHON" "$HERE/eval.py" --split "$SPLIT"
else
  exec "$PYTHON" "$HERE/eval.py" --split "$SPLIT"
fi
