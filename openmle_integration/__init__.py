"""OpenMLE integration package (Phase A scaffold).

Local, dependency-light re-implementation of the OpenRSI/OpenMLE `dojo` interface
contract, so safety_auto_research can align with the four atomic operators and the
program-level evolutionary search without pulling `dojo`'s heavy transitive deps
(aira_core / wandb / omegaconf / torch / mlebench / LLM clients).

Signatures mirror ``vendor/openmle_dojo/dojo/...`` exactly; see VENDORED_FROM.md.
"""
