# openmle_integration — OpenRSI/OpenMLE 接入 safety_auto_research 的接缝（Phase A）

本包是 Phase A 的交付物：**零依赖本地重实现** OpenRSI/OpenMLE 的 `dojo` 接口契约，
并把 safety 现有 tabular sklearn 流水线包装成 dojo 的 `Task`，跑通
`prepare → step_task → evaluate_fitness` 真·可验证任务环境对接。

> 合规：OpenMLE 为 **CC BY-NC 4.0**（非商用）。用户已确认不商用 → 可合法 vendoring。
> 上游 `dojo` 内核仅作参考副本存放于 `vendor/openmle_dojo/`（因依赖 aira_core/wandb/omegaconf，
> 整包导入会拖垮现有 venv，故此处重实现其契约，签名对齐，后续可零改指向真实 dojo 包）。

## 文件
- `contracts.py` — 镜像 dojo 的 `Task` ABC、outcome 常量（`TaskOutcome`/`AUX_EVAL_INFO`）、
  `Interpreter`/`ExecutionResult`、`Node`/`Journal`/`MetricValue`。**唯一对接面**。
- `interpreter.py` — 轻量 `PythonInterpreter`（多进程 + 超时），执行算子产出的程序（`action=str`）。
- `adapter.py` — `OpenMLETaskAdapter`：实现 `Task` 契约，包装 safety 的 tabular sklearn 流水线。
  - `action=str`  → 经 `PythonInterpreter` 执行 LLM/算子产出的程序，须写出 `submission.csv`。
  - `action=dict` → 内置 sklearn 管线（确定性），对应现有超参 config 进化。
  - `prepare` 留出 20% eval 集持真值，评分对真实标签 → 可验证环境。
- `tests/test_openmle_phase_a.py` — titanic 端到端 5 项测试（managed 3.13 venv 全通过）。

## 隔离不变量（必须保持）
- 本包只在内循环被调用（作为未来算子/executor 接缝），**不触发** `layer_11`/`layer_09`。
- 执行反馈只回流 `inner_params`，**不进** `audit_input`。
- 累积态（程序节点/经验/策略）必须带 `run_id` 过滤；adapter 自身不写归档，
  由 `orchestrator` 经 `control_plane.evolution.EvolutionArchive.add` 包裹。

## 下一步（Phase B+）
- B：四算子 `Draft/Improve/Debug/Crossover` 作为内循环能力接入（`assert_inner_capability_allowed` 校验）。
- C：`OpenMLE-Evo` 岛模型搜索接入 `orchestrator.run_evolutionary_loop`，候选写 `node_kind="program"` 节点。
- D：`OpenMLE-RL` 训练管线定位外接（无 GPU/docker + CC BY-NC），只移植 `reward_func` 语义。
