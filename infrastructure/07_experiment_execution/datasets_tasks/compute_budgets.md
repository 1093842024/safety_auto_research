# 计算资源配置（Compute Budgets）

实验执行层的资源规格与预算基线（来源：AutoLab README §System Requirements）。自建安全基准须据此声明预算，便于 log-scaled 评分对齐。

## 1. 四类任务硬件需求
| 类别 | 推荐硬件 | 备注 |
|------|---------|------|
| System Optimization | AMD Ryzen 9 9950X / 64GB 内存 | CPU 指令集差异影响最大 speedup；相对排名仍有效 |
| CUDA | H100 GPU | 推荐 Modal 等 GPU 容器 |
| Model Development | H100 或 L40S GPU | 推荐 Modal 等 GPU 容器 |
| Puzzle & Challenge | 无特殊硬件 | 任意 CPU 环境 |

## 2. 预算声明字段（建议）
- `max_flops` — 如 Claudini 预设 `1e15` / `1e16` / `1e17`
- `gpu_hours` / `wall_clock` — AutoLab 各任务含 compute budget
- `n_seeds` — 多次运行取均值±方差（设计层要求）

## 3. 预算与评分关系
- Log-scaled 评分基于 speedup（baseline→reference 对数插值，clip [0,1]），预算是锚定的分母。
- 超出预算的结果不计入排名（防「堆算力刷分」）。

## 4. 安全执行约束
- 红队执行：隔离容器 + `allow_internet=false`，不占用可外联资源。
- 预算须预注册；运行时监控超限即熔断。

> 引用：AutoLab `task.toml` 模板（层③ `autolab_tasks/`）已含 budget 字段，可直接复用。
