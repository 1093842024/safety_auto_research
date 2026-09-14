# spark-to-paper-skills 分析 与 引入 safety_auto_research 的集成方案

> 分析对象 A：`/Users/glennge/work/github/AI_research/spark-to-paper-skills`（Claude Code 插件，端到端论文生成 skill 套件，v1.2.0 / 1.1.0）
> 分析对象 B：`/Users/glennge/work/github/AI_research/safety_auto_research`（研究安全 harness：双循环 / 进化搜索 / 沙箱审计 / 评测榜单）
> 目标：① 分析 A 的核心机制 → ② 对比 B 现有机制，给出"更优之处 + 原因" → ③ 给出**不影响 B 稳定性与功能**的集成方案。

---

# 第一部分：spark-to-paper-skills 核心机制分析

## 1.1 定位与设计哲学

它是一个 **Claude Code 插件**（`.claude-plugin/plugin.json`），不是独立 Python 产品，也没有 app/server/数据库/Docker（"Zero infra"）。核心是 **1 个编排器 `ts-paper` + 13 个可组合 skill + 7 阶段流水线**，外加可选的 `ts-kg-build` / `ts-idea2story` 上游块。

设计哲学五条（README "Design Philosophy"）：

| 原则 | 含义 |
|---|---|
| 🧠 Model reasons | 判断类工作（写作/检索/批判/评审）由 Claude 负责 |
| 🛠 Code backstops | 确定性工作（lint/组装/绘图/矢量化）由 Python 兜底 |
| 🪶 Zero infra | 复制 skill 进 `.claude/skills/` 即用，无服务端 |
| 🏆 Quality first | 质量优先于成本，绝不跳过评审/编译 |
| 🔒 Integrity always | 绝不编造数字；每个值溯源到源数据；红门禁失败即停 |

最关键的一条工程信条贯穿全代码：**"The model does the reasoning. The code keeps it honest."** —— 用确定性代码给 LLM 的判断做"失效关闭（fail-closed）"的硬闸门。

## 1.2 流水线（Stage 0–8）

```
route(Stage0) → plan(1,blueprint.json) → cite(2,refs.bib) → write(3,sections/*.tex)
→ refine(4,de-AI+自查) → review(5,对抗评审) → figure(6,可编辑矢量图)
→ latex(7,main.pdf) → experiment(8,自动跑实验填表, AUTO)
```
两套完整性模式：`proposal`（严禁编造数字，结果表留 `--`）与 `data_aware`（每个数字必须溯源到用户真实数据，机器审计）。

## 1.3 核心可迁移机制（按价值排序）

### 机制 ① 统一确定性门禁运行器 `run_gates.py`
- **做什么**：`python run_gates.py <workdir> <stage|all>`。它**只消费**各 linter 的稳定退出码（0=ok / 1=issue / 2=usage），**不改任何门禁逻辑**，在**第一个非零退出码处停住**（red gate 即硬停，不越过）。
- **特点**：路径相对本文件解析（与 cwd 无关，适配 agent 线程重置 cwd）；必需输入缺失则 skip，必需工件缺失则是失败；纯 stdlib（`json/sys/subprocess/pathlib`）。
- **价值**：把散落的校验收敛成一个"单一可信闸门"，且"门禁逻辑"与"闸门调度"解耦——调度器永不变更校验语义。

### 机制 ② `audit_svg.py` —— 标准库几何审计（最可移植、最高价值的单文件资产）
- **做什么**：对"原生语义 SVG 图"做几何/结构审计。捕获的真实缺陷类：canvas 溢出（含被裁掉的箭头）、text-on-text、形状盖住文字（z-order）、`markerUnits="strokeWidth"` 导致箭头放大、悬空连线、过小字号、`✓` 类字形触发字体回退、嵌入 raster/data URI/ traced path-soup、presentation 属性被 stylesheet 覆盖（白字陷阱）。
- **惊艳之处**：**纯 stdlib（仅 `argparse/json/math/re/xml.etree/pathlib`），无需渲染器**——文字框用 Adobe core-14 Times 字宽表（与 Times New Roman / Nimbus Roman 度量兼容）精确测量，所以 SVG 几何审计不需要任何字体/PDF 引擎。自带 `--selftest`（9 类缺陷 + 裁切 marker + path-soup 全部命中，clean SVG 通过）。
- **价值**：这是一个**零依赖、可独立运行、自带回归自检**的"图形完整性门禁"。任何需要生成架构图/流程图/能力拓扑图并交付给人类审阅的场景都可直接复用。

### 机制 ③ 数据驱动的数字溯源 + 防编造 lint
- `draft_lint.py`：在**提案模式**禁止正文出现任何结果数字（除非是超参设计常量，有严格 lookbehind 豁免）；在 **data_aware 模式**，从 `results.facts.json`（用户真实数据的扁平数字集合）取"真值集"，正文里任何不在真值集的 decimal/percent 数字都报 `suspicious_number`。
- `citations_lint.py`：stub 检测（缺 author/year/venue/doi）、`\cite`↔`refs.bib` 交叉校验、孤儿条目、`claims_map.json` 的 claim↔cite 确定性匹配校验、全局引用下限 + 分章节覆盖带。
- **价值**：数字完整性是**确定性机器门禁**，而非 LLM 判断。这是 spark-to-paper 对"绝不编造"承诺的技术保障。

### 机制 ④ 对抗式评审引擎 `ts-paper-review`（PaperJury 精炼版）
算法（与执行层无关，三档 Tier 同算法同输出）：
1. **N 个隔离评审员**（默认 3 个镜头：理论/实证/系统），每人只读整篇论文原文，**隔离=论文是唯一输入**，每条弱点必须带**逐字引用**（"不能引用=没读=反略读"）+ 严重度 + `close_criterion`。
2. **合并去重**：相同问题折叠；无 `close_criterion` 的候选丢进 `dropped_no_criterion`（不可执行）。
3. **对抗式验证**：每个新问题面对 **3 个视角多元的怀疑者**（误读/已解决/范围-严重度），**多数反驳才丢弃**（≥2 驳回即丢），偏向"保留"。
4. **loop-until-dry**：连续 dry 轮或 round 上限后停。
- **引擎无关三档**：Tier1 Workflow 真实并行隔离 / Tier2 子 agent（真实隔离，等价质量）/ Tier3 in-context 模拟隔离（兜底）。**关键纪律：永远不会因缺少某工具就跳过评审**——算法与输出恒定。
- **账本纪律**：`logs/5_review.io.md` 的 OUTPUT 块**只在修复真正落地后**才写，且附捕获的 lint JSON + `date -u` 时间戳作为证据——"绝不预测关闭状态"。

### 机制 ⑤ 结构化逐步账本（INPUT/DECISIONS/OUTPUT）
每个阶段写 `logs/<n>_<stage>.io.md`，三段式：INPUT（消费的文件/文本）、DECISIONS（判断）、OUTPUT（工件路径+摘录）。契约是"镜像真实状态，不预测"。

### 机制 ⑥ 优雅降级哲学（多处贯彻）
- `novelty_check.py`：无 `TS_EMBED_*` 端点 / 无参考集时 **exit 0 写降级报告**，绝不硬失败——"语义检查诚实地可选"。
- `run_gates.py`：缺输入 skip 而非 fail；缺脚本则明确 FAIL（透明）。
- 视觉批判缺工具时降级到 in-context（Tier3）。
- 图 redraw 不收敛时：native redraw → DrawAI HYBRID → 保留 PNG，**永不降质重绘 / 永不平涂回归**。
- 与 B 已有 judge→heuristic 兜底同源，但 spark 把该哲学贯彻到更多子系统。

---

# 第二部分：与 safety_auto_research 的对比 —— 更优之处与原因

下表逐项给出"spark 哪点更优 / B 现状 / 为什么更优（证据）/ 迁移相关性"。

| # | 维度 | spark-to-paper 现状 | safety_auto_research 现状（来自代码探查） | 为什么 spark 更优 / 更值得借鉴 | 迁移相关度 |
|---|---|---|---|---|---|
| **C1** | **统一确定性门禁运行器** | `run_gates.py`：单一入口消费各 linter 退出码，**第一个 red gate 即停**，调度与校验解耦 | 只有**分散**的 pytest（仅开发期，未接入 live 循环）+ `audit_executor` 的程序化约束（eval-real / heldout-consistency / CV gate）。live 循环内**无统一 stop-on-red 运行器** | B 的校验散落在 pytest、audit_executor、service 多处；缺一个"把既有校验编排成 fail-closed 闸门"的轻量调度器。spark 的范式让"加门禁不碰门禁逻辑"且零依赖 | **高** |
| **C2** | **图形/SVG 完整性审计** | `audit_svg.py`：stdlib-only 几何审计，9+ 缺陷类 + selftest | **完全空白**（探查确认：核心平台不生成 SVG/矢量图，唯一"图"是 React 前端；`paper_verifier`/`paper-claim-audit` 是数字/LLM 判断，无矢量审计） | B 当前不画图，但**安全 harness 天然需要把双循环/岛模型拓扑、能力演化树渲染给人类审阅**。一旦引入图形工件，就缺审计。spark 的 `audit_svg.py` 可直接移植做"图是否真矢量、是否可读、是否溢出"的硬闸门 | **高**（作为前置能力储备） |
| **C3** | **数字溯源 + 防编造（确定性）** | `results.facts.json` 真值集 + `draft_lint` 机器审计每个正文数字 | 数字完整性靠 `paper_verifier`（infrastructure/researchclaw，正则+注册表，确定性）与 `paper-claim-audit`（**.claude skill，LLM 判断**）。**无"正文每个数字必须命中真值集"的确定性门禁** | B 的指标由 executor 真实测量（确定性好），但**人类可见报告层**若引用数字，无确定性重审计。spark 的 data-aware 溯源范式可补齐"报告/claim 工件"的确定性数字门禁 | **高** |
| **C4** | **对抗式"站在对面反驳"评审** | 隔离评审 + 逐字引用反略读 + 3 怀疑者多数反驳 + loop-until-dry，引擎无关三档 | `layer_11_external_audit` 是**只读策展式单一审计**（排除 inner 叙事以破自我确认，设计好），但**无"多视角反驳闭环"、无 loop-until-dry、无逐字引用反略读** | B 的 layer_11 解决"独立性"，spark 解决"对抗深度"。两者正交：可对 **audit findings / capability-safety claims** 叠加一个对抗加固层，降低安全漏判（false negative） | **中-高** |
| **C5** | **逐步账本"不预测关闭"纪律** | OUTPUT 仅在修复落地后写，附 lint JSON + 时间戳证据 | `ProgressBus`/SSE/`EventStream` 强于实时状态，但**不强调"证据化关闭"**（B 的 meta-loop patch 已做 falsifiable 验证，但 review 类修复无显式 close_criterion 绑定） | 借鉴 spark 的 `close_criterion` 绑定 + "落地后才记 green"纪律，让 B 的修复/评审闭环更可审计 | **中** |
| **C6** | **优雅降级贯通度** | novelty_check 缺端点不硬失败；gates 缺输入 skip；图不收敛逐级降级 | 已有 judge→heuristic 兜底、executor crash guard、sandbox 失败关闭 | 同源哲学，spark 贯彻更广。可借鉴其"缺依赖即降级而非崩溃"的注册模式，提升新模块健壮性 | **中** |
| **C7** | **claim↔cite 确定性预筛** | `claims_map.json` 确定性校验每条约化对应的引用质量/章节 | `claims_supported` 是 **LLM 判断**（默认 heuristic）。无确定性预筛 | 可用确定性 lint 先拦 off-topic/孤儿引用，再交 LLM 判断，降成本+提稳 | **中** |
| **C8** | **模板无关 / 插件结构** | `templates/<name>/` 丢目录即加 venue，无代码改动 | B 是 FastAPI 包 + `.claude/.codex` skill（Markdown 驱动，无 plugin.json） | 与 B 的论文/venue 概念无关，**低相关**，仅作为"可插拔"设计参考 | **低** |
| **C9** | **KG 召回 / 语义新颖性** | `ts-kg-build` 建研究模式 KG；`novelty_check` 校准带(0.88/0.82)+pivot cap，缺端点降级 | `novelty_filter`：config 余弦≥0.92 拒 + program 精确代码 set 去重（确定性好）；playbook/experience 是策展文本注入（仅内循环） | B 的新颖性已较强（余弦+精确代码双粒度），spark 的**语义 embedding 抗碰撞 + 校准带 + pivot 上限**可作为增强，但非关键缺口 | **低-中** |

**核心结论**：spark-to-paper 相对 B 的**代际优势**集中在三件事——(1) **把"确定性代码闸门"做成统一、解耦、零依赖的调度范式（C1）**；(2) **提供了图形/矢量完整性的标准库审计能力（C2，B 完全空白）**；(3) **用确定性机器门禁兜底"绝不编造数字/绝不编造引用"（C3/C7，B 偏 LLM 判断）**。其对抗评审（C4）与账本纪律（C5）是对 B 已有"独立性审计"的正交增强。

> 注意：B 的**沙箱隔离不变量（R9）、算子白名单、双循环审计独立性、 falsifiable meta-loop、确定性指标测量、cancel/budget/collaboration** 这些是其**已有且更强**的部分，spark 没有对应物——集成时**必须不触碰**这些，只做增量叠加。

---

# 第三部分：优点引入集成方案（不破坏稳定性）

## 3.1 集成总原则（硬约束）

1. **增量 + 隔离**：只**新增文件/新增 opt-in 端点/新增 skill**，绝不修改 `execution_plane/`、`control_plane/` 的现有逻辑、路由、沙箱、算子白名单。
2. **默认关闭（opt-in）**：新能力通过 feature flag（如 `INTEGRITY_GATES=0`）默认 OFF，绝不进入双循环/进化循环的默认热路径。
3. **零新依赖**：移植件只用 stdlib（复用现有 venv 的 numpy 即可），不引入 torch/transformers 等重依赖，避免污染 managed venv。
4. **只读边界对齐**：任何评审/审计新模块只消费**策展后的 audit_input（结果指标）**，绝不读 inner-loop 事件——对齐 `layer_11` 的策展边界，不破坏"隔离不变量"。
5. **收尾汇聚遵守 R9**：若新增后台端点，必须走现有 `deps.release_run_slot`（内含 `progress_bus.close()` 发 EOF 关 SSE），不另起收尾。
6. **独立回归测试**：新增 `tests/test_integrity_suite_*.py`，不改动现有测试；跑新增测试套件独立、不进全量，避免 SIGKILL。
7. **可一键回退**：删除新包/新端点即完全回退，对 B 零影响。

## 3.2 目标结构（全部为新增）

```
safety_auto_research/
├── integrity_suite/                      # 【新增】纯函数可移植门禁套件（stdlib-only）
│   ├── __init__.py
│   ├── svg_audit.py                       # 移植 audit_svg.py（零依赖，自带 --selftest）
│   ├── gate_runner.py                     # 移植 run_gates.py 范式：包装 B 既有校验为 fail-closed 闸门
│   ├── adversarial_review.py             # 移植 ts-paper-review 算法（隔离评审+逐字引用+3怀疑者+loop-until-dry）
│   ├── number_trace.py                    # 移植 data-aware 数字溯源 lint（可选，校验报告/claim 工件）
│   └── claims_lint.py                     # 移植 claims_map 确定性预筛（可选）
├── control_plane/routers/integrity.py    # 【新增】build_integrity_router(deps)，仅 opt-in 端点
└── .claude/skills/integrity-gate/        # 【新增】与现有 auto-review-loop 并列的 skill（不替换）

tests/test_integrity_suite_svg.py         # 【新增】svg_audit selftest 复跑 + B 图样
tests/test_integrity_suite_gates.py       # 【新增】gate_runner 包装既有校验的回归
```

## 3.3 各模块落地细节

### 模块 A：`integrity_suite/svg_audit.py`（直接移植，最高优先级）
- 动作：把 `spark-to-paper-skills/skills/ts-figure-svg/scripts/audit_svg.py` 复制为 `integrity_suite/svg_audit.py`，**仅改 import 路径无关项（它纯 stdlib，无需改）**。保留 `--selftest`。
- 为什么零风险：零依赖、单文件、自带自检；B 当前不调用它（没有图生成）。它只是"能力储备"——只有当 B 未来新增图形工件时才被引用。
- 验证：`python -m integrity_suite.svg_audit --selftest` 应通过（与 spark 同源通过）。

### 模块 B：`integrity_suite/gate_runner.py`（移植 C1 调度范式）
- 动作：移植 `run_gates.py` 的"消费退出码 + 第一个非零即停 + 路径相对本文件解析 + 缺输入 skip"范式。它**不做新校验**，只把 B **既有**确定性校验编排成闸门：
  - 包装 `control_plane/evolution.novelty_filter`（config 余弦 / program 精确代码去重）——作为"新颖性闸门"；
  - 包装 `execution_plane/capabilities/audit_executor.evaluate_constraint`（程序化约束：eval-real / heldout-consistency / CV gate）——作为"审计闸门"；
  - 包装 `service._recompute_top3` / `leaderboard` 的方向感知（复用 `_select_objective`，修 R12 的方向不一致）——作为"榜单一致性闸门"。
- 关键：**gate_runner 永不修改这些既有函数的逻辑**，只调用它们并据返回值/退出码决定停否。这是 spark "调度与校验解耦" 的核心。
- 调用时机：仅 opt-in 端点或 CI/预发布校验触发；**不进** `ClosedLoopOrchestrator.run_dual_loop` 默认路径。

### 模块 C：`integrity_suite/adversarial_review.py`（移植 C4，正交增强）
- 动作：移植 ts-paper-review 的对抗算法为纯 Python 函数 `run_adversarial_review(paper_text, venue_profile, results_mode, tier)`。三档中：
  - Tier2（子 agent）/ Tier3（in-context）可直接用 B 现有 agent 能力；
  - 算法与输出结构恒定（issue 对象 `{severity,section,evidence_quote,close_criterion,raised_by}`）。
- 边界（不破坏隔离不变量）：输入只接受**策展后的 audit_input / capability-safety claims 文本**，**绝不直接读 inner-loop 事件流**；输出建议回写 `layer_11` 的"加固项"，由现有 `decide_and_record` 决定 Accept/Refine/Restart——即**作为 layer_11 的前置加固层，而非替代它**。
- 默认：仅当用户显式请求"对抗加固"或 opt-in 端点触发时运行；**不接入默认双循环**。

### 模块 D：`integrity_suite/number_trace.py` + `claims_lint.py`（移植 C3/C7，可选）
- 动作：`number_trace` 提供 `results.facts.json` 真值集 + 正文数字命中检查（适配 B：把"真值集"取为 executor 真实测量的指标/评测结果，而非论文数据）。
- `claims_lint` 提供 claim↔cite 确定性预筛，拦 off-topic/孤儿引用后，再交 B 现有 `_default_judge` / 真实 LLM judge。
- 范围：**仅对人类可见报告/claim 工件**生效（如 `paper-claim-audit` 的工件），不改动 executor 内部指标计算。

### 接入点：`control_plane/routers/integrity.py`（opt-in，默认 OFF）
- 仿 `build_loops_router(deps)` 模式新增 `build_integrity_router(deps)`，暴露：
  - `POST /workflow-runs/{id}/verify-integrity`：对**已完成**的 run 跑 `gate_runner`（包 novelty/audit/leaderboard 闸门）+ 可选 `number_trace`/`claims_lint`。
  - 受 `INTEGRITY_GATES` 环境变量门控：未开启时端点返回 `403 disabled`，不执行任何校验。
- 注册：在 `control_plane/api.py` 照现有 `app.include_router(build_router(deps))` 模式追加一行 `app.include_router(build_integrity_router(deps))`——**这是唯一允许的极小改动**，且纯新增、不触碰现有路由体。
- 收尾：若需后台执行，复用 `deps._spawn_bg_thread` + `release_run_slot`（遵守 R9 收尾汇聚）。

### Skill 侧：`.claude/skills/integrity-gate/`（不替换现有）
- 新增一个 skill，引导 agent 在"用户要产报告/评审/加固"时调用 `integrity_suite` 的确定性门禁，**与现有 `auto-review-loop`、`paper-claim-audit`、`integrity-forensics` 并列共存**，不删除/不改写它们。

## 3.4 分阶段路线

| 阶段 | 动作 | 风险 | 校验 |
|---|---|---|---|
| **Phase 0** | 新增 `integrity_suite/` 空包 + `svg_audit.py` 移植；跑其 `--selftest` | 极低（零依赖、无调用方） | `python -m integrity_suite.svg_audit --selftest` 通过 |
| **Phase 1** | 新增 `gate_runner.py`，包装 `novelty_filter`/`evaluate_constraint`/`_select_objective` 为闸门（纯调用，不改逻辑） | 低（不改既有函数） | `tests/test_integrity_suite_gates.py` 对既有校验做回归 |
| **Phase 2** | 新增 `routers/integrity.py` + `api.py` 一行 include + `INTEGRITY_GATES` 开关；默认 OFF | 低（纯新增端点，默认 403） | 现有全量回归 `pytest` 不变绿；新端点默认 disabled |
| **Phase 3** | 新增 `adversarial_review.py` + `number_trace.py` + `claims_lint.py` + skill | 中（仅 opt-in 触发） | 新增单测；不进默认循环 |

## 3.5 冲突与"不可执行"规避清单（针对用户顾虑）

- ✅ **不动热路径**：`run_dual_loop` / `run_evolutionary_loop` / `run_program_evolutionary_loop` 默认行为零改动；新门禁只在 opt-in 端点/显式 skill 触发。
- ✅ **不动沙箱不变量**：R9 attestation、`AGENT_SANDBOX_MARKER_PATH`、`assert_operator_inner_only` 白名单、`layer_11` 只读策展边界**原样保留**；`adversarial_review` 只读策展输入，不读 inner 事件。
- ✅ **不引入依赖**：移植件纯 stdlib（+ 复用已有 numpy），不污染 managed venv（无 torch/transformers）。
- ✅ **不修改既有测试**：新增 `test_integrity_suite_*.py` 独立跑；不进全量避免 SIGKILL（全量一次性加载会 exit 137）。
- ✅ **门禁逻辑不被调度器改**：`gate_runner` 只消费返回值/退出码，绝不改写 `novelty_filter` / `evaluate_constraint` / `_select_objective` 内部——避免"优化变成行为破坏"。
- ✅ **API 真实性**：新端点沿用现有 `build_*_router(deps)` + `include_router` + `_spawn_bg_thread`/`release_run_slot` 真实签名（已核对 `loops.py` 与 `api.py`），不是臆测接口。
- ✅ **一键回退**：删除 `integrity_suite/`、`routers/integrity.py`、`api.py` 那一行即完全回退，对 B 零残留影响。

## 3.6 风险与对策

| 风险 | 对策 |
|---|---|
| 新端点若误默认开启，可能拖慢/干扰既有 run | `INTEGRITY_GATES` 默认 0；端点未开启返回 403；仅对**已完成** run 运行 |
| `adversarial_review` 若误接 inner 事件会破坏隔离不变量 | 函数签名强制要求传入策展后的 `audit_input`；加单测断言其不接收 event-stream 对象 |
| 移植 `audit_svg.py` 与 B 无图形工件，"用不上" | 作为能力储备；Phase 0 仅验证 selftest 通过，不要求立即有调用方；未来图形化（拓扑/演化树渲染）即可直接用 |
| 过度集成导致与现有 `auto-review-loop`/`integrity-forensics` 重复 | 明确定位：`integrity_suite` 是**确定性代码闸门**，`auto-review-loop` 等是 LLM 判断层，两者互补不替代；skill 文案写明分工 |

---

# 附：一句话总结

spark-to-paper-skills 真正领先 safety_auto_research 的，不是"论文生成"本身，而是三件**通用质量工程资产**：① 统一、解耦、零依赖的 fail-closed 门禁调度范式；② 标准库级的图形/矢量完整性审计；③ 用确定性机器门禁兜底"不编造数字/不编造引用"。这三项都可以通过**纯新增、零依赖、opt-in、默认关闭、不改热路径与沙箱不变量**的方式引入 B，在完全不触碰其现有稳定性与功能的前提下，给 B 补上"图形审计空白"与"报告层确定性完整性"两块短板，并对 layer_11 做正交的对抗加固。
