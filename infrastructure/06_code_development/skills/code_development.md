# Skill：代码开发 Runbook（Code Development）

> 把实验设计方案落地为可运行、可测试、可评审的代码。多 Agent 协作模式参考 MLEvolve（#1 on MLE-bench）。

## 循环（迭代直到通过质量门）
1. **Draft**（`draft_agent`）— 基于节点方案生成初始实现（可走 `coder/base_coder` 整文件 / `stepwise_coder` 分步 / `diff_coder` 增量补丁）。
2. **Code Search**（`code_searcher`）— 检索仓库内可复用模式，避免重复实现、对齐约定。
3. **Improve**（`improve_agent`）— 针对测试/评审反馈单分支迭代改进。
4. **Debug**（`debug_agent`）— 自动定位并修复运行/测试失败。
5. **Review**（`code_review_agent`）— 静态审查（见 `eval_methods/code_quality.md`）。
6. **Evolve**（`evolution_agent`，可选）— 多分支停滞时跨分支进化改进。

## 约束
- 仅改动隔离工作树（Arbor worktree / Harbor 容器），不污染主分支。
- 所有外部依赖显式声明（参考 `datasets_tasks/code_scaffold.md`）。
- 安全代码：无硬编码密钥、无红队目标明文、评审覆盖注入/越权面。
