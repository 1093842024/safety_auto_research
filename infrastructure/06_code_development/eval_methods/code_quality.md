# 代码质量评估（Code Quality）

代码开发环节产出在进入「实验执行」前须通过以下质量门控（由 `code_review_agent` 执行，可作为自动红线）。

## ① 可运行（Runnable）
- 在目标环境无导入/语法错误；入口可被调用。
- 依赖完整（见 `datasets_tasks/code_scaffold.md` 依赖清单）。

## ② 测试通过（Tests Pass）
- 单元/集成测试非空且通过；新增功能有对应测试。
- 复现脚本（`solve.sh` / `train.sh`）可在干净环境跑通。

## ③ 规范（Style & Convention）
- 通过 lint / type check；命名与仓库约定一致（`code_searcher` 检索到的模式）。
- 无死代码、无调试残留。

## ④ 无数据泄露（No Leakage）
- 训练/测试严格分离；无标签泄露、无未来信息泄漏。
- 与层⑧ `data_leakage_agent` 共享经验：引用其历史命中规则。

## ⑤ 安全红线（Security Gates）
- 无硬编码密钥/凭证（密钥走 `credentials.*.env`）。
- 无红队目标/载荷明文。
- 注入/越权/资源外泄面经评审覆盖。

> 评分：①–③ 为硬性 pass/fail；④–⑤ 为安全强制项，任一失败即阻断并回退到代码开发循环。
