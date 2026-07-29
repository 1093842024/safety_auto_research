# 文献检索 Agent 规范（安全 Auto-Research）

> 本目录沉淀「文献检索」基础设施层中的 Agent 角色定义与可复用源码。
> 配套 skill 见 `../skills/`，数据源与质量评估见 `../datasets_tasks/`、`../eval_methods/`。

## 1. 角色定位

文献检索 Agent（Search Agent）负责把"研究问题"转化为"结构化、去重、可溯源的论文集合"，
为下游 idea 生成与评估校验提供证据底座。它不直接产出想法，而是保证后续所有声明都能
`track back` 到具体检索记录（这是 ResearchStudio-Idea 的 Citation Gate 与 ARIS 的
claim 层依赖链的前提）。

## 2. 已沉淀的可复用源码

### `arbor_search_agent/`（来自 Arbor）
- `agent.py`（148 行）：检索 Agent 主体，封装 alphaXiv API 调用 + 新颖性初筛。
- `prompts.py`（311 行）：检索 / 新颖性判定的 system prompt。
- `main.py`（118 行）：命令行入口（setup/run）。
- `__init__.py`：模块导出。

**调用契约**
```
python arbor_search_agent/main.py --query "<研究问题>" --year-from 2021
```
输出：去重后的论文列表（title / id / year / venue / 新颖性初判）。

## 3. 跨项目统一检索 Agent 设计（建议）

综合 ARIS 的 8 个检索 skill 与 ResearchStudio 的 `paper-search`（6 源并发），
安全场景的检索 Agent 应支持如下能力矩阵：

| 能力 | 实现载体（已拷贝 skill） | 适用场景 |
|------|------------------------|----------|
| 多源并发检索 | `paper_search`（arXiv/DBLP/OpenAlex/OpenReview/Semantic Scholar/Crossref） | 通用文献综述、prior-art |
| 单源精检 | `arxiv` / `semantic-scholar` / `openalex` / `deepxiv` | 精确到单库的深挖 |
| 专利 / 先验艺术 | `prior-art-search` | 工程落地前查重、规避设计 |
| 商业 / 网络检索 | `exa-search` / `alphaxiv` | 最新预印本、alphaXiv 社区讨论 |
| 综述整合 | `comm-lit-review` / `research-lit` | 把检索结果组织成 narrative |
| 知识库沉淀 | `research-wiki` / `wiki-enrich` | 把论文/想法/实验/声明持久化为图谱 |

## 4. 安全领域适配要点

- **对抗性检索**：安全研究目标动态变化（攻击方/防御方博弈），检索 query 需覆盖
  攻击术语 + 防御术语 + 评测术语三套词汇，避免只看一侧。
- **合规约束**：内容安全 / 数据隐私相关检索须标注监管来源（如法规、标准、红队报告），
  并在 `research-wiki` 中打 `compliance` 标签。
- **可复现**：所有检索必须记录查询串、年份窗口、命中的源与时间戳，写入运行目录
  （参考 idea_spark 的 `lit_results.json` + `user_refs.json`），保证声明可溯源。

## 5. 与下游的接口

```
Search Agent
   └─> 论文集合 (JSON) ──> idea 生成层 (Citation Gate 引用)
                         └─> 评估与基准层 (先验艺术碰撞 / scoop-check)
```
