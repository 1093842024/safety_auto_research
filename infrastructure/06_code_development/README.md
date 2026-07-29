# 基础设施层 ⑤：代码开发（Code Development）

> 统一梳理「代码开发」环节的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》§3.2（MLEvolve agents、AutoResearchClaw code_searcher）、§4.7（MLEvolve 多 Agent 协作）。

## 1. 定位

把实验设计落成的协议转化为**可运行、可测试、可评审**的代码变更。对应通用流程闭环中的 `代码生成` 节点。

## 2. Skill（已梳理至 `skills/`）

- `code_development.md` — 代码开发 runbook：草稿→改进→评审→调试的迭代循环，引用 MLEvolve 多 Agent 协作模式。

## 3. Agent（已拷贝至 `agents/`）

- `mle_evolve_code/` — **MLEvolve #1 代码开发角色**（多 Agent 协作）：
  - `draft_agent.py`（初始方案生成）、`improve_agent.py`（单分支迭代改进）
  - `evolution_agent.py`（停滞时分支进化改进）、`code_review_agent.py`（代码审查）
  - `debug_agent.py`（自动调试）、`coder/`（`base_coder.py` / `diff_coder/` / `stepwise_coder.py` — 三种代码生成策略）
- `code_searcher/` — **AutoResearchClaw 代码检索 agent**：
  - `agent.py` / `github_client.py` / `pattern_extractor.py` / `query_gen.py` / `cache.py`
  - 从仓库/代码库检索可复用模式，支撑「站在已有实现上改进」而非从零生成。

## 4. 数据集任务（已梳理至 `datasets_tasks/`）

- `code_scaffold.md` — 代码脚手架与质量基线：测试骨架、lint/type 配置、依赖清单（参考 MLEvolve `requirements_*.txt`）。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `code_quality.md` — 代码质量门控：`code_review_agent` 标准（可运行 / 测试通过 / 规范 / 无数据泄露 / 无硬编码密钥）。

## 6. 安全领域适配

- 禁止在代码中硬编码凭证/红队目标；密钥走环境变量（`credentials.*.env`，参考 ARC-Bench）。
- 代码评审强制检查**注入/越权/资源外泄**面（与安全执行层共享 `data_leakage_agent` 经验）。
- 攻击代码生成受控：仅在隔离工作树（Arbor worktree / Harbor）内，不进入主分支直至评审通过。
