# MLE-bench 数据下载 — 竞赛规则接受与下载记录

> 状态：✅ **全部 9 个竞赛已下载+准备成功**（2026-07-30 14:27 完成），数据落 `benchmark_tasks/suites/data/mle_bench_data/<id>/{raw,prepared/public,prepared/private}`，总计约 3.2GB。
> Kaggle `kaggle.json` 已验证有效；竞赛规则已全部在 kaggle.com 手动接受。

## 已完成下载的 9 个竞赛（lite ∩ 数据量 <100MB* ∩ 非已知问题）

| # | 竞赛 | 数据量(GB) | 规则接受页 | 状态 |
|---|------|-----------|-----------|------|
| 1 | aerial-cactus-identification | 0.0254 | https://www.kaggle.com/c/aerial-cactus-identification/rules | ✅ 已下载+准备 (87M) |
| 2 | denoising-dirty-documents | 0.06 | https://www.kaggle.com/c/denoising-dirty-documents/rules | ✅ 已下载+准备 (217M) |
| 3 | detecting-insults-in-social-commentary | 0.002 | https://www.kaggle.com/c/detecting-insults-in-social-commentary/rules | ✅ 已下载+准备 (3.1M) |
| 4 | jigsaw-toxic-comment-classification-challenge | 0.133* | https://www.kaggle.com/c/jigsaw-toxic-comment-classification-challenge/rules | ✅ 已下载+准备 (133M) |
| 5 | leaf-classification | 0.036 | https://www.kaggle.com/c/leaf-classification/rules | ✅ 已下载+准备 (29M) |
| 6 | nomad2018-predict-transparent-conductors | 0.00624 | https://www.kaggle.com/c/nomad2018-predict-transparent-conductors/rules | ✅ 已下载+准备 (15M) |
| 7 | spooky-author-identification | 0.0019 | https://www.kaggle.com/c/spooky-author-identification/rules | ✅ 已下载+准备 (3.2M) |
| 8 | text-normalization-challenge-english-language | 0.344* | https://www.kaggle.com/c/text-normalization-challenge-english-language/rules | ✅ 已下载+准备 (344M) |
| 9 | text-normalization-challenge-russian-language | 0.517* | https://www.kaggle.com/c/text-normalization-challenge-russian-language/rules | ✅ 已下载+准备 (517M) |

> * 标注 `*` 的 3 个竞赛（jigsaw / text-normalization-en / text-normalization-ru）实测 prepared 体积超过原 manifest 估算的 100MB 阈值（原估算只计了部分/原始 zip）。数据已下载且可用；如需严格 <100MB，可删除这 3 个的 `mle_bench_data/<id>` 目录。

## 如何重新运行 / 扩展下载
1. 用与 `~/.kaggle/kaggle.json` 相同的 Kaggle 账号登录 https://www.kaggle.com
2. 若要新增更大的 lite 竞赛，需在 kaggle.com 逐个**手动接受规则**（Kaggle 不提供接受规则的 API），然后调整 `acquire_mle_bench_lite.py` 的 `size_gb_limit` 或目标清单
3. 重跑命令：
```bash
export KAGGLE_CONFIG_DIR=/Users/glennge/.kaggle
cd /Users/glennge/work/github/AI_research/safety_auto_research
/Users/glennge/.workbuddy/binaries/python/envs/default/bin/python \
  benchmark_tasks/suites/data/acquire_mle_bench_lite.py </dev/null
```
数据落盘：`benchmark_tasks/suites/data/mle_bench_data/<competition_id>/{raw,prepared/public,prepared/private}`
报告：`mle_bench_acquire_report.json`（list，每项含 public/private 路径；9/9 prepared = 成功）

## 按 <100MB 约束**未下载**的更大 lite 竞赛（如需可放宽阈值或单独指定）
aptos2019-blindness-detection (10.22G)、dogs-vs-cats-redux-kernels-edition (0.85G)、histopathologic-cancer-detection (7.76G)、mlsp-2013-birds (0.585G)、new-york-city-taxi-fare-prediction (5.7G)、plant-pathology-2020-fgvc7 (0.8G)、siim-isic-melanoma-classification (116.16G)、tabular-playground-series-dec-2021 (0.7G)、tabular-playground-series-may-2022 (0.57G)、the-icml-2013-whale-challenge-right-whale-redux (0.293G)。（ranzcr-clip 既超 100MB 又被已知问题剔除）
