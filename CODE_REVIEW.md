# safety_auto_research Code Review 报告

**审查日期**: 2026-07-29  
**审查范围**: `safety_auto_research` 全项目（Python 后端 + TypeScript 前端）  
**测试基线**: 81/81 passed | **前端 tsc**: 0 errors

---

## 摘要

共发现 **25 个功能缺陷**（3 Critical, 12 Important, 9 Minor, 1 结构问题）。

测试套件全部通过、前端类型检查干净，但核心风险集中在：
1. **异常处理链断裂** — `_drive()` 双层 `except Exception: pass` 导致 run 永久卡死 + SQLite 连接泄漏
2. **并发安全** — 读写锁缺失导致状态读不一致
3. **前端类型安全** — API 响应未经运行时校验，后端变更即可触发前端崩溃
4. **前端显示 Bug** — DualLoopLive 视图硬编码进度上限 + 外循环轨迹全部显示 `undefined`

---

## 按模块分类

### 一、控制平面 (control_plane/)

| # | 文件:行 | 严重级别 | 问题 | 影响 |
|---|---------|---------|------|------|
| C1 | `api.py:844-850` | 🚨 Critical | 双层 `except Exception: pass` 吞没 `_drive()` 所有异常 | run 失败后永远卡在 "running" 状态；调用者收到 HTTP 201 但实际已死 |
| C2 | `store_tree.py:49,68,76,215,221,229,282,288,296` (9 处) | 🚨 Critical | SQLite 连接未用 `with` 上下文管理器，异常时泄漏 | 高并发下 `database is locked` → 连接耗尽 → 服务不可用 |
| I1 | `store.py:56-58` | ⚠️ Important | `_load` 中 `except Exception: return` 静默吞掉 corrupt 文件 | 进程被 KILL 后 store JSON 损坏 → 所有历史数据不可逆丢失 |
| I2 | `service.py:74,413,458` | ⚠️ Important | `_open_approvals` dict 仅内存中，不持久化 | 重启后所有审批请求被遗忘 → run 永久卡 WAITING_APPROVAL 死锁 |
| I3 | `store.py:109-188` | ⚠️ Important | `list_*` 读方法不加锁，写方法持锁写 | 并发 read-while-write 读到不一致数据（dict.values() 视图迭代未定义行为） |
| M1 | `store.py:62-66` | 💡 Minor | 条目重建 `except Exception: continue` 静默丢弃损坏记录 | 重启后"丢了几个 run"，无日志可追踪 |
| M2 | `eval_runner.py:171` | 💡 Minor | `json.loads(ranked)` 未捕获 JSONDecodeError | LLM 返回非标准 JSON → 500 内部错误 |

### 二、执行平面 / Orchestrator (execution_plane/)

| # | 文件:行 | 严重级别 | 问题 | 影响 |
|---|---------|---------|------|------|
| I4 | `kaggle_eval_executor.py:80,130` | ⚠️ Important | `cv_folds` 未校验上限，可能 > min class size | 小数据集 + 默认 folds → sklearn ValueError → run 失败 |
| I5 | `kaggle_eval_executor.py:253` | ⚠️ Important | `df[target]` 不校验列名存在性 | 用户拼错 target 列名 → KeyError → 被 C1 吞没 → run 僵尸 |
| I6 | `service.py:108` (`_STAGE_EVENT_TYPE` 映射) | ⚠️ Important | `to_status` KeyError 当枚举扩展新值时 | stage 状态变更但事件未发 → 内部状态与 event log 不对齐 |
| I7 | `orchestrator.py:408-412` | ⚠️ Important | `metrics.get("accuracy")` 假设 metrics 是 dict | agent 返回非 dict metrics → AttributeError → 被 C1 吞没 |
| M3 | `agent/transport.py:177-185` | 💡 Minor | `proc.wait()` 无超时 | 子进程僵死 → 调用线程永久阻塞 |

### 三、前端 (frontend/)

| # | 文件:行 | 严重级别 | 问题 | 影响 |
|---|---------|---------|------|------|
| C3 | `views/DualLoopLive.tsx:43` | 🚨 Critical | `const maxOuter = 3` 硬编码，忽略用户配置 | 用户设 5 轮 → 始终显示 0/3, 1/3… 完全错误的进度信息 |
| I8 | `views/DualLoopLive.tsx:153-155` | ⚠️ Important | 外循环轨迹访问不存在的 `e.event_type` → 全部 `undefined` | 审计/改进历史列表完全空白 |
| I9 | `api/client.ts:7,17` | ⚠️ Important | 裸 `as T` 类型断言无运行时校验 | 后端 JSON 结构变更 → 前端静默崩溃（`TypeError: Cannot read property .map of null`） |
| I10 | `views/ApprovalConsole.tsx:56-69` | ⚠️ Important | 拒绝操作无确认对话框，不可逆 | 误点"拒绝" → 关键研究流程立即终止 |
| I11 | 7 个视图 (轮询 catch) | ⚠️ Important | 所有轮询 `catch {/*ignore*/}` → 网络故障无声 | 后端停机期间用户看到过期数据，误判研究状态 |
| I12 | `views/RegisterTask.tsx:216-348` | ⚠️ Important | 多路径字段上传写入同一 key → 覆盖 | 同时上传 train/eval 数据 → 第二个覆盖第一个 |
| M4 | `views/RunDashboard.tsx:49-54` | 💡 Minor | `DECISION_LABEL` 缺少 `"continue"` | 显示原始英文字符串无样式 |
| M5 | `views/DualLoopLive.tsx:112-115` | 💡 Minor | `metrics.accuracy` 在 non-accuracy 指标下为 `undefined` | 显示 `acc=undefined gate=true` |
| M6 | `views/Leaderboard.tsx:59-66` | 💡 Minor | 混合方向排序不一致 | "全部任务"模式下排名语义混乱 |
| M7 | `views/Leaderboard.tsx:167` | 💡 Minor | 奖牌用 sorted 数组索引而非任务内排名 | 在同任务视图中奖牌显示不准确 |
| M8 | `App.tsx:91` | 💡 Minor | `getBenchmarkTasks` 失败后静默降级 | 所有 run 名显示原始 task_id |
| M9 | `views/NewResearch.tsx:557-650` | 💡 Minor | agent CLI 配置在切回 scripted 后残留 | payload 携带多余 `agent_cli` 字段 |

### 四、结构问题

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| S1 | 包根目录 `safety_auto_research/` | 缺少 `__init__.py` | Python 3.13 命名空间包可工作，但旧工具/静态分析器可能报错 |

---

## 安全扫描结果

✅ **无误提交密秘**：所有 API key/token 均通过 `os.environ.get()` 或配置对象读取，无硬编码。  
✅ **无敏感文件**：`data/` 下只有运行时状态文件（已加入 `.gitignore`）。

---

## 修复优先级建议

### 立即修复（P0）
- 🔴 **C1** — `api.py` `_drive()` 异常吞没：加 `logging.exception` 至少保证可观测
- 🔴 **C2** — `store_tree.py` SQLite 连接泄漏：全部改为 `with sqlite3.connect(...) as conn:`
- 🔴 **C3** — `DualLoopLive.tsx` 硬编码 `maxOuter`

### 近期修复（P1）
- 🟡 **I1–I7** — 数据可靠性 + 边界条件
- 🟡 **I8–I12** — 前端显示正确性 + 操作安全性

### 后续迭代（P2）
- 🟢 **M1–M9** — 边缘场景展示优化
- 🟢 **S1** — 添加 `__init__.py`
