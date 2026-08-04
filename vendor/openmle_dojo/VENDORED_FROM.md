# Vendored: OpenMLE `dojo` kernel (reference copy)

This directory contains a **faithful reference copy** of the `dojo` runtime kernel from
**OpenRSI / OpenMLE** (Frontis AI), used by the *Frontis-MA1* paper
(arXiv:2607.28568, "Training an AI4AI Model towards Recursive Self-Improvement in ML Engineering").

## Provenance
- Upstream repo: `https://github.com/FrontisAI/OpenRSI` (branch `main`)
- Vendored path in upstream: `OpenMLE-Evo/third_party/aira-evo/src/dojo/`
- Vendored on: 2026-08-04
- This copy **excludes** `__pycache__/` and internal `test_*.py` to prevent accidental
  collection by this project's pytest suite.

## License & attribution (NON-COMMERCIAL use confirmed)
- **OpenMLE (top-level)**: Creative Commons Attribution-NonCommercial 4.0 International
  (`LICENSE_openmle_cc_by_nc_4.0.txt`). **Non-commercial use only.** The safety_auto_research
  project uses this exclusively for research (non-commercial) — confirmed 2026-08-04.
- **`dojo` kernel (aira-evo)**: original code © Meta Platforms, Inc. and affiliates
  (`LICENSE_aira_evo.txt`), with additional upstream licenses in `THIRD_PARTY_LICENSES.md`
  (WecoAI / DeepMind / OpenAI / etc.).
- `NOTICE_openmle.txt` preserves the upstream OpenRSI notice.

## IMPORTANT — why this is a reference copy, not an imported package
The upstream `dojo` package imports heavy transitive dependencies that are **not installed**
in this project's runtime environment (`aira_core`, `wandb`, `omegaconf`, `torch`, `mlebench`,
LLM clients, etc.). Importing the vendored `dojo` directly would break the environment.

Therefore:
- This vendored copy is kept **only as a source-of-truth reference** for the interface contract
  (class/function signatures, outcome-key constants, operator semantics, evo solver design).
- The *consumable* interface is a **dependency-light re-implementation** under
  `safety_auto_research/openmle_integration/` (`contracts.py`, `adapter.py`, `interpreter.py`)
  whose signatures mirror `dojo` exactly. When this project later gains the heavy deps (or a
  slim runtime), the local contract can be swapped for `from vendor.openmle_dojo.dojo...`
  with no change to the integration code.

## What was reused vs. re-implemented
| Upstream (`dojo`) | In this project | Status |
|---|---|---|
| `core/tasks/base.py::Task` (ABC) | `openmle_integration/contracts.py::Task` | Re-implemented (faithful signature) |
| `core/tasks/constants.py` outcome keys | `openmle_integration/contracts.py` constants | Re-implemented (same names) |
| `core/interpreters/{base,python}.py` | `openmle_integration/interpreter.py` | Re-implemented (subprocess, timeout) |
| `core/solvers/utils/{journal,metric}.py` | `openmle_integration/contracts.py` | Re-implemented (Node/Journal/MetricValue) |
| `core/solvers/operators/*.py` (4 operators) | Phase C (operators as inner-loop capabilities) | Planned |
| `core/solvers/evo/evo.py` (island model) | Phase C (evolutionary search) | Planned |
| `tasks/mlebench/{task,evaluate}.py` | `openmle_integration/adapter.py` (sklearn wrapper) | Adapted for local titanic |
