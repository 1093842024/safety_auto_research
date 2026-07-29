# 基础设施层 ①：文献检索（Literature Search）

> 统一梳理「文献检索」层的 **skill / agent / 数据集任务 / 评测方式**，并沉淀安全 auto-research 可复用资产。
> 来源：《AI_Research_Infrastructure_Report.md》Layer 2（研究执行层-文献检索）、ResearchStudio Paper-Search、ARIS 8 个检索 skill、Arbor Search Agent。

## 1. 定位

把"研究问题"转化为"结构化、去重、可溯源的论文集合"，为下游 idea 生成（Citation Gate）与
评估校验（先验碰撞）提供证据底座。

## 2. Skill 清单（已拷贝至 `skills/`）

| Skill | 来源 | 功能 | 体积 |
|-------|------|------|------|
| `paper_search` | ResearchStudio | 6 源并发检索+去重（arXiv/DBLP/OpenAlex/OpenReview/Semantic Scholar/Crossref），含脚本 | 84K |
| `research-lit` | ARIS | 多源文献检索主入口 | 44K |
| `arxiv` | ARIS | arXiv 精检 | 12K |
| `semantic-scholar` | ARIS | Semantic Scholar 检索 | 12K |
| `openalex` | ARIS | OpenAlex 检索 | 12K |
| `deepxiv` | ARIS | DeepXiv 全文/预印本 | 8K |
| `prior-art-search` | ARIS | 先验艺术检索 | 8K |
| `exa-search` | ARIS | 商业/网络检索 | 12K |
| `alphaxiv` | ARIS | alphaXiv 社区讨论 | 12K |
| `research-wiki` | ARIS | 论文/想法/实验/声明知识库沉淀 | 20K |
| `wiki-enrich` | ARIS | 知识库富化 | 20K |
| `comm-lit-review` | ARIS | 综述整合 | 12K |

## 3. Agent（已拷贝至 `agents/`）

- `arbor_search_agent/` — Arbor 文献检索 Agent 源码（`agent.py`/`prompts.py`/`main.py`），alphaXiv API + 新颖性初筛。
- `agents/README.md` — 跨项目统一检索 Agent 设计（能力矩阵 + 安全适配要点）。

## 4. 数据集任务（已梳理至 `datasets_tasks/`）

- `literature_sources_catalog.md` — 7 个连接器矩阵、时间窗口策略（idea_spark Phase 0 非重叠窗口）、
  去重/相关性截断规则、安全领域检索查询模板。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `retrieval_quality.md` — 召回完整性 / 去重正确性 / 相关性 / 可溯源 四维评估框架 +
  失败模式（API 漏检、限流、词汇盲区）+ Citation/Full-text Gate + 安全约束。

## 6. 安全领域适配

- 覆盖攻击/防御/评测三套术语检索；合规来源打 `compliance` 标签。
- 所有检索记录查询串、年份窗口、时间戳，保证声明可溯源、合规可审计。
