# 文献检索质量评估方式

> 文献检索层本身没有"benchmark 分数"，其质量通过**召回完整性 / 去重正确性 / 相关性 / 可溯源**
> 四个维度评估。本文件统一这些评测约定，供安全 auto-research 复用。

## 1. 四维评估框架

| 维度 | 含义 | 评测信号 | 来源 |
|------|------|---------|------|
| **召回完整性 (Recall)** | 是否漏掉关键论文 | 用户锚定论文是否都被命中；模型记忆补充项是否必要 | `paper_search` model-knowledge 兜底 |
| **去重正确性 (Dedup)** | 跨源重复是否被合并 | 同一 paper 在不同源出现时只计一次 | `paper_search` SS-priority 去重 |
| **相关性 (Relevance)** | 命中是否与查询相关 | BM25 重叠度截断；≤120 hits/channel | idea_spark Phase 3.1 |
| **可溯源 (Traceability)** | 声明能否回到具体检索 | 每条 claim 关联 `lit_results.json` paper_id | ARIS claim 层 / Citation Gate |

## 2. 失败模式与对策

- **API 漏检经典文**：靠 `model_knowledge` 源补 foundational / 跨域经典，但强制 `(uncertain — verify)` 标注，禁止伪造引用。
- **arXiv 限流 (429)**：`paper-search` 用跨进程文件锁 `arxiv_search_throttled.lock`，相邻请求 ≥4s；勿直连绕过。
- **关键词盲区**：solution-vocabulary escape query（检索"已修复该瓶颈"的论文，它们常以 fix 命名，problem-keyed query 抓不到）。
- **词汇盲区（lexical blind spot）**：别名碰撞通道用 48 月窗口（如 goal-conditioned success detector vs goal-image conditioned scorer），仅扩 signature 窗口抓不到。

## 3. 质量门（Gate）

- **Citation Gate（idea 层前置）**：idea 中每个 sub_pattern 引用必须来自真实读过的 overview.md，而非从父模式 gist 猜测 → `validate --phase2` 确定性校验。
- **Full-text Gate（Phase 0+ 强制）**：`lit_table.md` 落地后立即 `phase0_fulltext`，Phase 1 硬门 `error: fulltext_not_fetched`，避免只凭摘要做判断。

## 4. 安全领域额外约束

- 检索结果须记录**查询串 / 年份窗口 / 命中源 / 时间戳**，写入运行目录，保证合规可审计。
- 涉及内容安全 / 数据隐私的检索结果打 `compliance` 标签并标注监管来源。
- 攻击方法类检索须同时覆盖攻击/防御/评测三套术语，避免单侧重捕。
