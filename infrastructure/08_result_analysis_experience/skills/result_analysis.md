# Skill：结果分析 Runbook（Result Analysis）

> 把执行证据转为可信结论与可复用经验。统计与声明侧复用层② `result-to-claim`/`experiment-audit` 与层④ `stat_research_agent`。

## 流程
1. **解析**（MLEvolve `result_parse_agent`）— 从日志/指标文件提取结构化指标（loss/curve/score），对齐种子维度。
2. **校验**（MLEvolve `data_leakage_agent` + 层② `experiment-audit`）— 检查训练/测试泄露、未来信息、评估偏差。
3. **统计**（层④ `stat-comparison-analyst` / `stat-result-synthesizer`）— 显著性检验、置信区间、与基线对比。
4. **声明生成**（层② `result-to-claim`）— 产出可证伪声明（claim + evidence + 边界）。
5. **融合/聚合**（MLEvolve `fusion_agent` / `aggregation_agent`）— 跨分支合并洞察，停滞检测触发进化。
6. **经验沉淀**（MLEvolve `memory/` + Arbor `arbor_backpropagate`）— 归纳洞察写入全局记忆，向上传播至假设树。

## 红线
- 同时报告攻击成功率 **与** 防御侧安全性降级（ASR ↔ Safety trade-off）。
- 红队目标/载荷明文不进入记忆明文；以脱敏指纹存储。
- 被证伪声明标 `refuted`，沉淀为「已知无效方向」。
