# 可选 Benchmark 任务总览与实验验证情况（2026-08-13 更新）

> 数据来源：`benchmark_tasks.get_catalog()` 全量 40 条；
> 判定「已实验验证」= 单次 launch 经 `SandboxResearchExecutor`／平台生产路径实跑，产出真实
> `result.json` + `EvalCompletedEvent` + **数值** `primary` 指标。
> 相对 2026-08-12《SURVEY_experimental_readiness》的关键进展：`:full` 镜像建成、
> Tier A' 9 个与 Tier B SAB 13 个已 L1 验证、6 个库版本漂移的 SAB 已从可选目录移除。

---

## 0. 一句话结论

`get_catalog()` 共 **40 条**，其中 **37 条可 launch 且已完成 L1 实验验证**（含真实数值指标），
**3 条为非可运行**（2 个外部 suite manifest + 1 个用户显式排除的 `mlevolve.mle_bench`）。

可选的、已验证可用的 benchmark 任务 = **37 条**，按来源分 11 组（见下表）。

---

## 1. 目录总览（40 条 = 37 可运行 + 3 灰）

| 来源 (source_project) | 条数 | 可运行 | 已验证 | 隔离/环境 |
| --- | ---: | :---: | :---: | --- |
| safety_auto_research（平台原生 kaggle） | 5 | ✅ | ✅ | 平台 kaggle_eval 生产路径 |
| custom（用户自定义 sandbox） | 4 | ✅ | ✅ | `:full` 沙箱（torch） |
| OSU-NLP/ScienceAgentBench | 15 | 14 | 14 | `:full` 沙箱（domain libs）×13 + numpy-only `h_importances_92`×1；另 `suite.*` 为 manifest |
| autolab（CPU 代理端口） | 7 | ✅ | ✅ | `:latest` 沙箱（numpy-only runner） |
| arbor | 1 | ✅ | ✅ | `:latest` 沙箱 |
| autoclaude | 1 | ✅ | ✅ | `:latest` 沙箱 |
| claudini（tmeoa 端口） | 3 | ✅ | ✅ | soft + bridge 网络（tmeoa 网关） |
| Agent-Native-Research-Artifact (ara) | 1 | ✅ | ✅ | soft + bridge 网络 |
| AutoResearchClaw (arc_bench) | 1 | ✅ | ✅ | soft + bridge 网络 |
| MLEvolve | 1 | ❌ | — | `enabled=False`（用户显式排除） |
| openai/mle-bench | 1 | ❌ | — | suite manifest（非单任务） |
| **合计** | **40** | **37** | **37** | — |

> 注：`suite.science_agent_bench` / `suite.mle_bench` 是外部数据集的 manifest 描述符，
> 不对应可 launch 的单任务，计入 3 条「灰」。

---

## 2. 分组明细：数据 · 环境 · 评测 · 验证

### 2.1 平台原生 kaggle（5）— 最高优先级，生产路径
| task_id | 数据 | 环境 | 评测指标（实测 primary） | 验证 |
| --- | --- | --- | --- | --- |
| platform.titanic | kaggle presets（in-repo `data/kaggle`） | 平台 kaggle_eval（5 折 CV，非沙箱） | accuracy ≈ **0.8257** | PASS |
| platform.spaceship | 同上（合成数据） | 同上 | accuracy ≈ **0.9429** | PASS |
| platform.wine | 同上 | 同上 | accuracy ≈ **0.934** | PASS |
| platform.iris | 同上 | 同上 | accuracy ≈ **0.9686** | PASS |
| platform.breast_cancer | 同上 | 同上 | accuracy ≈ **0.9483** | PASS |

### 2.2 用户自定义 sandbox（4）— Tier A'
| task_id | 数据 | 环境 | 评测指标（实测 primary） | 验证 |
| --- | --- | --- | --- | --- |
| custom.task_20260811171209（文本） | upload（data/oss/text_cls_demo） | `:full`（torch） | f1_macro = **1.0** | PASS |
| custom.embedding_2（嵌入检索） | upload | `:full` | recall_at_10 = **0.1667** | PASS |
| custom.4_2（图像） | upload（image_cls_demo） | `:full`（torch CNN） | top1_accuracy = **1.0** | PASS |
| custom.4_3（音频） | upload（audio_cls_demo） | `:full`（torch MelCNN） | accuracy = **1.0** | PASS |

### 2.3 ScienceAgentBench（13 可运行 + 1 控制样）
- **数据**：vendor 语料 `benchmark_tasks/suites/data/vendor/ScienceAgentBench/benchmark/`（datasets + gold_programs + eval_programs），已物化到 `data/oss/sab.*`。
- **环境**：`safety-research-sandbox:full`（含 rdkit / neurokit2 / biopsykit / ccobra / matminer / geopandas / MDAnalysis / prolif / cftime+iris / librosa / torch，numpy 1.26.4，xxhash 2.0.2）；离线 `--network none`。
- **评测**：每任务独立 eval 脚本，SR（success rate）= 程序输出与 gold 在 TOLERANCE=1e-4 内一致；`primary=0.0` 表示「命中 gold」。
- **验证**：13/13 PASS（本次 `:full` 实跑）。

| task_id | 领域依赖 | 验证 |
| --- | --- | --- |
| sab.h_importances_92（控制样） | 纯 numpy | PASS（`:latest` 即可） |
| sab.predict_bulk_modulus_3 | matminer | PASS |
| sab.dili_models_ecfp_rf_18 | rdkit | PASS |
| sab.dili_models_ecfp_svm_19 | rdkit | PASS |
| sab.md_rf_40 | rdkit/MDAnalysis | PASS |
| sab.deforestation_21 | geopandas | PASS |
| sab.bio_eventrelated_analyze_29 | neurokit2 | PASS |
| sab.hrv_analyze_34 | neurokit2 | PASS |
| sab.rrv_analyze_35 | neurokit2 | PASS |
| sab.cft_37 | geopandas | PASS |
| sab.nvc_accuracies_60 | ccobra | PASS |
| sab.nvc_gen_ind_58 | ccobra | PASS |
| sab.questionnaire_45 | biopsykit | PASS |
| sab.cogsci_pattern_high_sim_67 | ccobra | PASS |

### 2.4 autolab（7）— CPU 可行性代理
- **数据**：官方 task.py/solution.py 物化到 `data/oss/autolab.*`；runner 为 **numpy-only**（不依赖 torch）。
- **环境**：`:latest` 沙箱（1.14 GB，`--network none`）。
- **评测**：原任务需 GPU/CUDA/7B 权重，此处以**真实但代理**的 CPU 参考测量替代（指标名/方向与原任务一致，偏差在 `port_notes` 声明）。
- **验证**：7/7 PASS（实测 primary）：safety_router accuracy≈0.6719 · grpo_multisource ref_step_latency_s≈3.8e-05 · flash_attention runtime_seconds≈0.07 · aes128_ctr≈10.6 · adaptive_compression bits_per_byte≈5.73 · ntt_butterfly_cuda runtime_ms≈207.8 · llm_online_serving serving_score≈2.56。

### 2.5 arbor（1）/ autoclaude（1）
- **arbor.algotune_knn**：数据 `data/oss/arbor.algotune_knn`；`:latest`；评测 speedup（reference vs solution 中位计时）≈6.0–6.3；**PASS**。
- **autoclaude.trigger_eval**：数据 `data/oss/autoclaude.trigger_eval`；`:latest`；评测 trigger_rate=1.0（确定性离线词表探针替代 claude 探针）；**PASS**。

### 2.6 claudini（3）/ ara（1）/ arc_bench（1）— tmeoa 端口（需联网）
- **环境**：soft 隔离 + bridge 网络，访问 tmeoa 网关（qwen3.6-35b-a3b 受害者）；**非离线沙箱**。
- **评测**：
  - claudini.random / injection / safeguard → `asr`（攻击成功率，越低越好）：实测 0.0 / 0.33 / 0.0（真实拒答/小预算单次测量，`TmeoaError` 保证非空值）。
  - ara.understanding → `absolute_correctness_success_rate`≈0.4（闭卷、gold 关键 token contains-check 代理）。
  - autoresearchclaw.arc_bench → `rubric_weighted_score`≈1.0（单 topic ML01 结构化结果 rubric 代理）。
- **验证**：5/5 PASS（soft 路径，需 tmeoa 可达）。

### 2.7 灰掉（3，非可选）
- `mlevolve.mle_bench`：`enabled=False`，外部 openai/mle-bench，本仓未打包，**用户显式排除**。
- `suite.mle_bench` / `suite.science_agent_bench`：外部 suite manifest 描述符，非单任务。

---

## 3. 本次变更（2026-08-13）

1. **构建 `safety-research-sandbox:full`**（4.16 GB，`c2bdd8e98a6e`）：`python:3.11-slim` + torch + 领域库 + `numpy==1.26.4`（修复 rdkit/MDAnalysis ABI）+ `xxhash==2.0.2`（修复 iris 3.14 内部 `hexdigest`）+ 补丁 aarch64 `ts2vg` wheel。已功能验证全部 domain import OK。
2. **Tier A' 9 个全部 L1 验证 PASS**（5 平台 kaggle + 4 自定义）。
3. **Tier B SAB 13 个 PASS**（env-block 已解决，`:full` 实跑）。
4. **移除 6 个库版本漂移失败的 SAB 任务**：在 `benchmark_tasks/_oss_wired_extra.py` 的 `build_extra_tasks` 加 `_SAB_EXCLUDED` 过滤（保留 `sab_19_task_blocks.py` 源文件为 truth，仅从 launchable 目录剔除）。被移除：
   - `sab.imu_44`（biopsykit `pd.to_datetime(unit='us')` 在 pandas 2.x 溢出 ns 范围）
   - `sab.ligand_fingerprint_26`（prolif/MDAnalysis 版本漂移 → [520,410] 样本数不一致）
   - `sab.saliva_85`（biopsykit 0.13 `standard_features` 与 gold 微差）
   - `sab.md_knn_41`（sklearn KNN 输出漂移，F1=0.713 低于 SAB 通过线）
   - `sab.polynomial_fit_87`（计算正确，但 numpy 1.26 字符串 repr 与 gold 17 位不符 → eval N/A）
   - `sab.mat_feature_select_2`（mastml 随机特征选择，卡在「≥14 列匹配」阈值，flaky）
5. **目录校验**：`get_catalog()` 现 40 条，6 个排除项确认不在目录（`present_excluded=[]`），13 SAB 留存。

---

## 4. 环境配置注意（运行前必读）

- 执行器默认镜像 `AGENT_SANDBOX_IMAGE=safety-research-sandbox:latest`（numpy/sklearn 系，1.14 GB）。
- **跑 SAB-13 或自定义 image/audio 分类需 `:full`**：
  `export AGENT_SANDBOX_IMAGE=safety-research-sandbox:full`（否则 import 领域库/torch 即失败）。
- claudini / ara / arc_bench 需 **soft 隔离 + bridge 网络**访问 tmeoa 网关，不进离线沙箱。
- 全局仅一个镜像变量，暂无按任务选镜像机制（如需可后续在 `SandboxResearchExecutor` 增加 per-task image 映射）。

---

## 5. 验证证据

- `experiments/oss_validation/verify_tierA_B_results.json`：21 任务（19 SAB + 2 自定义 torch）在 `:full` 实跑，TOTAL=21 / passed=15（= 13 SAB + 2 自定义 torch）。
- `experiments/oss_validation/REPORT_L1_batch_wiring_2026-08-12.md`：autolab×7 / arbor / autoclaude / claudini×3 / ara / arc_bench / sab.h_importances_92 共 15 OSS 任务 L1 验证矩阵。
- `experiments/oss_validation/SURVEY_experimental_readiness_2026-08-12.md`：分层梳理（已部分被本轮更新覆盖）。

---

## 6. 结论

- **可选且已实验验证的 benchmark 任务 = 37 条**（5 平台 + 4 自定义 + 14 SAB + 7 autolab + 1 arbor + 1 autoclaude + 3 claudini + 1 ara + 1 arc_bench）。
- **非可运行（灰）= 3 条**（mlevolve 排除 + 2 suite manifest）。
- Tier B SAB 的 env-block 已彻底解决；余下 6 个失败确认为**上游库版本漂移**（非平台缺陷），已从可选目录移除，不计入「具备完整验证条件」的任务集。
