# 实验执行可复现性评估（Execution Reproducibility）

执行层的核心不止「跑通」，而是「**别人按相同输入能复现相同证据**」。本层质量门控（与层③ `scoring.md` 互补）。

## ① 隔离（Isolation）
- 容器/工作树隔离；`allow_internet=false`（红队强制）。
- 无宿主依赖泄漏；环境由 `Dockerfile`/`docker-compose.yaml` 完全声明。

## ② 确定性（Determinism）
- 随机种子固定并随证据记录；并行/异步操作有可复现顺序。
- 数据划分（train/val/test）固定且来自声明数据源（防 `data_leakage_agent` 命中）。

## ③ 可溯源（Provenance）
- 代码 commit / 环境指纹 / 参数三者绑定到每条证据。
- 环境快照可重建（Harbor 镜像或 lockfile）。

## ④ 防污染（Anti-contamination）
- `harbor-canary` GUID 注入；若评测/训练数据中出现该 GUID，判定污染并作废结果。
- 红队目标/载荷明文不得进入日志、记忆或任何下游。

## ⑤ 证据完整性（Evidence Completeness）
- `reward.json` / 指标文件齐全；失败 case 有错误类型标注（供调试/修复循环消费）。

> 评分：①–④ 任一缺失 → 结果不可信，回退重跑或退回代码开发层；⑤ 不全 → 进入自愈（`nanoresearch_execution/repair_*`）再评估。
