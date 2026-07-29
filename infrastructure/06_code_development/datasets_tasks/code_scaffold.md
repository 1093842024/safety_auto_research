# 代码脚手架与质量基线（Code Scaffold）

代码开发环节复用的骨架与依赖基线。

## 1. 依赖基线（来自 MLEvolve `requirements_*.txt`）
- `requirements_base.txt` — 通用 ML/优化依赖（torch、numpy、scikit-learn、pandas…）
- `requirements_domain.txt` — 领域专用（CV/NLP/表格）
- `requirements_ml.txt` — Kaggle 竞赛相关

## 2. 测试骨架
- `tests/test.sh` 约定（继承自 AutoLab `task.toml` 标准）：运行 benchmark → 计算 reward → 写 `reward.json`
- 单元测试 + 复现脚本双轨：单元保证正确性，复现脚本保证端到端可跑

## 3. 代码生成策略（来自 `agents/mle_evolve_code/coder/`）
- `base_coder` — 整文件生成（适合从零模块）
- `stepwise_coder` — 分步生成（适合长文件/复杂逻辑）
- `diff_coder` — 增量补丁（`apply.py` / `diff_generate.py` / `patcher.py` / `prompts.py`）

## 4. 复用检索约定（来自 `code_searcher`）
- 先 `query_gen` 生成检索式 → `github_client` 拉取候选 → `pattern_extractor` 抽模式 → `cache` 命中复用
- 目标：站在已有实现上改进，而非重复造轮子

> 安全适配：脚手架默认启用密钥占位符与 `.env` 加载；禁止提交真实凭证。
