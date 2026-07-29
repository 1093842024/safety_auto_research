# 文献数据源连接器清单（数据集 / 任务）

> 文献检索层的"数据集"即各学术检索源连接器；"任务"即给定查询串 → 去重论文集合的检索任务。
> 本清单蒸馏自 `../skills/paper_search`（6 源并发）与 `../skills/research-lit`（idea_spark Phase 0 检索窗口）。

## 1. 连接器矩阵

| 源 | key | 检索内容 | 备注 |
|----|-----|---------|------|
| arXiv | `arxiv` | 预印本 | 标准库即可，无需 API key |
| DBLP | `dblp` | 计算机论文元数据 | 期刊/会议索引强 |
| OpenAlex | `open_alex` | 全学科论文 + 引用图 | 无需 key |
| OpenReview | `openreview` | ICLR/NeurIPS/ICML 在审稿 | 需 `openreview-py` |
| Semantic Scholar | `semantic_scholar` | 摘要 + 引用数 | 无需 key（可选 key 提额） |
| Crossref | `crossref` | DOI 元数据 | 出版级元数据 |
| Model Knowledge | `model_knowledge` | 模型自身记忆回忆 | 无 API，须诚实标注 `(uncertain — verify)` |

## 2. 时间窗口策略（来自 idea_spark Phase 0）

为保证"近期高信号 + 历史根基"兼顾，采用非重叠窗口并发检索：

| 源 | 时间窗口 | 上限 | 优先级 |
|----|---------|------|--------|
| arXiv | 0–6 月 | 10 | — |
| OpenReview | 0–6 月（在审） | 10 | — |
| OpenAlex | 6–24 月 | 12（仅已发表） | SS-priority 去重 |
| Semantic Scholar | 6–24 月 | 13（仅已发表） | SS-priority 去重 |

单源失败不影响其他源（catch + 续跑 + 报告 stderr）。

## 3. 去重与相关性截断

- **跨源去重**：按 title 归一化（大小写/空白）去重；Semantic Scholar 优先作为去重锚。
- **相关性截断**：碰撞检索时按通道 relevance 截断（≤120 hits/channel，BM25 重叠度），
  零相关 BM25 噪声无条件丢弃；未截断池保留为 `collision_hits.full.json`。
- **用户锚定论文**：查询中按 TITLE 点名的论文用 `add_user_ref` 确定性合并入 `user_refs.json`。

## 4. 安全领域检索任务模板（示例查询集）

可在 `../skills/paper_search/scripts/search_papers.py` 上拼装，建议覆盖三套词汇：

```text
# 攻击侧
"LLM jailbreak attack method 2024"
"prompt injection defense 2025"
# 防御侧
"content moderation classifier robustness"
"safe RLHF reward model safety"
# 评测侧
"red teaming benchmark LLM safety"
"automated safety evaluation agent"
```

## 5. 输出 Schema（供下游消费）

```json
{
  "arxiv":        [ {"title":, "authors":, "year":, "abstract":, "url":, "venue":, "citation_count":} ],
  "semantic_scholar": [ ... ],
  "open_alex": [ ... ],
  "openreview": [ ... ],
  "crossref": [ ... ],
  "dblp": [ ... ],
  "model_knowledge": [ ... ]
}
```
