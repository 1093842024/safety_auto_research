# Skill：实验执行 Runbook（Experiment Execution）

> 在隔离、可复现环境中真实运行实验并收集证据。容器化底座复用层③ AutoLab Harbor 沙箱。

## 流程
1. **准备隔离环境** — `harbor run -p <task>` 或 Docker：`allow_internet=false`，挂载代码与数据卷。
2. **注入 canary** — 写入 `harbor-canary` GUID，后续检测评测是否污染训练数据。
3. **运行** — 复现脚本（`solve.sh`/`train.sh`/`run_single_task.sh`）在容器内执行；固定随机种子。
4. **收集证据** — `tests/test.sh` 计算 reward/指标，写 `reward.json`；结构化日志（指标+错误+环境指纹）。
5. **回传** — 证据回灌上层（Arbor Executor→Coordinator；MLEvolve→Global Memory；AutoResearchClaw→Analysis 阶段）。
6. **清理** — 销毁容器/工作树，保留指标与快照。

## 引擎选择
- 多分支搜索/优化 → MLEvolve `engine/`（UCT 选择 + 隔离执行 + 反向传播）
- 长程流水线 → AutoResearchClaw `researchclaw_pipeline`（22 阶段）
- 假设树单点验证 → Arbor `arbor_executor`
- GPU 重计算 + 自愈 → NanoResearch `nanoresearch_execution`

## 红线
- 红队执行仅限隔离沙箱；目标/载荷明文不得落盘或外传。
- 任何执行失败须先 `repair_*` 自愈，超阈值再回退代码开发层。
