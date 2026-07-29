# 经验 / 记忆 Schema 与分析模板（Memory Schema）

经验生成环节落地的记忆格式与分析 I/O 模板。

## 1. 全局记忆记录格式（来自 MLEvolve `agents/memory/record.py`）
```python
MemoryRecord:
    id: str                 # 唯一标识
    task: str               # 关联任务/领域
    problem: str            # 问题陈述（脱敏）
    solution_summary: str   # 方案要点
    metric: float           # 关键指标
    insight: str            # 归纳洞察（可复用）
    code_ref: str           # 代码/实验证据引用
    tags: list[str]         # 检索标签（方法/数据集/失败模式）
    created_at: str
```
- 索引：BM25（关键词）+ FAISS（向量），由 `global_memory.py` / `retriever.py` 提供检索。

## 2. 分析 I/O 模板
- **输入**：执行层证据（指标文件 + 环境指纹 + 种子）+ 假设树节点（目标/基线）。
- **处理**：`result_parse_agent`（解析）→ `data_leakage_agent`（校验）→ 统计检验 → `fusion_agent`/`aggregation_agent`（跨分支）。
- **输出**：结构化结论（claim + evidence + 边界）+ 新 `MemoryRecord`（洞察向上传播）。

## 3. Arbor 经验回传（来自 `arbor_backpropagate/`）
- `convergence.py`：监控 score velocity，detect plateau → 触发决策（合并/剪枝/继续/终止）。
- `idea_tree.py`：把节点 `insight` 沿父链向上传播，祖先节点据此更新计划。

## 4. 安全约束
- `problem` / `insight` 字段对红队目标做脱敏（占位符代替具体载荷）。
- `refuted` 类型记录：标记「已知无效方向」，检索命中时优先剪枝。
- 记忆淘汰：长期低命中且过时的记录归档，避免记忆膨胀误导新设计。

> 与层④ `design_templates.md` 的假设树 schema、层⑦执行证据回传形成「设计→执行→分析→记忆→再设计」闭环。
