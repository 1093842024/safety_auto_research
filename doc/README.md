# doc/ 设计文档索引

> 本目录是 `safety_auto_research` 的设计/分析文档总索引。**按类别 + 状态**组织，便于快速判断"哪份是当前权威、哪份已归档"。
> 最后整理：2026-08-07（文档树重构：审查系列合并为活文档、旧快照归档、删除重复文件）。

---

## 维护约定（避免文档膨胀）

1. **代码审查**只更新 [`code_review_STATUS.md`](code_review_STATUS.md) 一份活文档，不再新建带日期的审查文件；必须保留历史时移入 `archive/`。
2. **研究调研 / 设计蓝图**为方向性参考，不随代码频繁改动；落地后以文内"状态/差距"横幅标注，不重写全文。
3. **与代码强绑定的文档**（`benchmark_tasks.md`、`architecture_diagram.html`）须标注"最后同步日期"，代码变更后手动同步，以代码为准。
4. **过时文档**移入 `archive/`，不立即删除；仅当确认零外部引用且为重复件时才删（如已删的 `Deep_Dive_Direction2_3_RL_Extension.md`）。

---

## 一、活动待办（Active）

| 文件 | 状态 | 说明 |
|---|---|---|
| [`product_ux_backlog.md`](product_ux_backlog.md) | 🟢 进行中 | 产品/UX/架构 18 条待办（F1–F7 / U1–U4 / FL1–FL4 / UI1–UI6 / A1–A4），由首轮深度分析报告提炼，可靠性类已排除 |

## 二、代码审查（权威）

| 文件 | 状态 | 说明 |
|---|---|---|
| [`code_review_STATUS.md`](code_review_STATUS.md) | 🟢 活文档 | **全量代码审查与修复状态（唯一权威）**。含 08-05 全量审查 + 附录 A（暂缓项 L1–L7 裁决落地）+ 附录 B（08-06 修复核实 + F1–F6）。后续审查只更新此文件 |

## 三、架构与设计（参考，部分已落地）

| 文件 | 状态 | 说明 |
|---|---|---|
| [`design_notes.md`](design_notes.md) | 🟢 活文档 | **功能设计单一权威**：控制平面拆分+DI（§1，A1/A3/F1）、审计追问协议（§2，F6）、无 Docker agent 隔离（§3，F2/F3）。新设计在此追加分节，不再新建独立 design_*.md |
| [`unified_safety_rd_platform_architecture_spec.md`](unified_safety_rd_platform_architecture_spec.md) | 🟡 Draft v1 | 目标架构 spec；写于 MEA/OpenRSI 落地前，文内已标"与现实差距" |
| [`dual_loop_upgrade_plan.md`](dual_loop_upgrade_plan.md) | 🟡 设计参考 | AREX 双循环升级方案，部分已落地 |
| [`harness_gap_analysis_and_upgrade_plan.md`](harness_gap_analysis_and_upgrade_plan.md) | ✅ 已落地 | Weng Harness 差距分析，三期全部落地 |
| [`mea_harness_upgrade_plan.md`](mea_harness_upgrade_plan.md) | ✅ 已落地 | MEA（Manage-Execute-Audit）控制循环升级蓝图 |
| [`architecture_diagram.html`](architecture_diagram.html) | 🟢 活文档 | 架构图（含 §⑨ 待闭环状态）；与代码同步更新 |

## 四、研究调研（参考，不随代码更新）

| 文件 | 状态 | 说明 |
|---|---|---|
| [`content_moderation_agent_report.md`](content_moderation_agent_report.md) | 📚 参考 | 5 方向总调研母本 |
| [`AI_Research_for_safety.md`](AI_Research_for_safety.md) | 📚 参考 | 方法论映射（内容审核垂域） |
| [`Adversarial_safety_multi_Agent.md`](Adversarial_safety_multi_Agent.md) | 📚 参考 | 方向三 RL 对抗（权威，被 infrastructure 多处引用） |
| [`streaming_safety_audit_deep_research.md`](streaming_safety_audit_deep_research.md) | 📚 参考 | 流式实时安全审核调研 |

## 五、基准任务（与代码绑定，需同步）

| 文件 | 状态 | 说明 |
|---|---|---|
| [`benchmark_tasks.md`](benchmark_tasks.md) | 🟡 需同步 | 18 任务目录（镜像 `benchmark_tasks/__init__.py`），文内标同步日期 |
| [`benchmark_suites_integration.md`](benchmark_suites_integration.md) | 🟡 需同步 | ScienceAgentBench / MLE-bench 套件集成说明 |

## 六、历史归档（`archive/`）

| 文件 | 原日期 | 说明 |
|---|---|---|
| [`archive/code_review_2026-07-30_round3.md`](archive/code_review_2026-07-30_round3.md) | 07-30 | 早期全量审查，已被 08-05 全量审查覆盖 |
| [`archive/code_review_2026-08-04.md`](archive/code_review_2026-08-04.md) | 08-04 | 后端+OpenRSI 审查，其修复已在 08-05 标注"已修复" |
| [`archive/safety_auto_research_深度分析报告.md`](archive/safety_auto_research_深度分析报告.md) | 07-29 | 首轮深度分析快照，可靠性章节已过时，有效内容见 `product_ux_backlog.md` |

> 已删除：`Deep_Dive_Direction2_3_RL_Extension.md`（与 `Adversarial_safety_multi_Agent.md` 重复，且无任何外部引用）。
> 已删除：`design_control_plane_split.md` / `design_audit_followup.md` / `design_agent_sandbox.md`（三份功能设计已并入 `design_notes.md` 活文档，零外部引用，于 2026-08-07 删除）。
> 另：`safety_auto_research_深度分析报告.md` 已于 2026-08-07 归档于此（见上表），为 2026-07-29 首轮分析快照；其可靠性章节已过时，有效内容见 `product_ux_backlog.md`。
