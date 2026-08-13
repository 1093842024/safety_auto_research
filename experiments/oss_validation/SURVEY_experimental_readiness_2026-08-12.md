# Benchmark 任务「实验验证条件」完整分层梳理 (2026-08-12)

> 判定标准「具备完整实验验证条件」= **enabled（可 launch）＋ 有真实 runner ＋ 已 L1 实跑产出数值指标（result.json + EvalCompletedEvent，numeric `primary`）**。
> 数据来源：`get_catalog()` 全量 46 条（`_TASKS`=42 ＋ `_custom_tasks`=4）。

---

## 一、Tier A —— 已 L1 验证（完整实验验证条件）✅ 共 15 个

均为本次 OSS 接通 + 样板工作实跑确认，单次 launch 即产出真实数值指标。

### A1. 硬隔离（offline, docker `--network none`）— 11 个

| task_id | 类别 | eval_metric（实测 primary） | L1 结果 |
| --- | --- | --- | :---: |
| `autolab.safety_router` | model_dev | accuracy≈0.6719（unsafe_recall=.697/safe_recall=.647/params=16641）| PASS |
| `autolab.grpo_multisource` | model_dev | ref_step_latency_s≈3.8e-05 | PASS |
| `autolab.flash_attention` | system_opt | runtime_seconds≈0.07 | PASS |
| `autolab.aes128_ctr` | system_opt | runtime_seconds≈10.6 | PASS |
| `autolab.adaptive_compression` | puzzle | bits_per_byte≈5.73 | PASS |
| `autolab.ntt_butterfly_cuda` | cuda | runtime_ms≈207.8 | PASS |
| `autolab.llm_online_serving` | model_dev | serving_score≈2.56 | PASS |
| `arbor.algotune_knn` | efficiency | speedup≈6.0–6.3 | PASS |
| `autoclaude.trigger_eval` | tooling | trigger_rate=1.0 | PASS |
| `sab.h_importances_92` | agent_eval | max_abs_error≈4e-16（纯 numpy 控制样）| PASS |
| `autoresearchclaw.arc_bench` ⚠️ | agent_eval | rubric_weighted_score≈1.0 | PASS（**soft/联网**）|

> ⚠️ `arc_bench` 虽列硬隔离段但实测走 **soft + bridge 网络**（访问 tmeoa），因 topic ML01 的 rubric 评估需联网；其余 10 个确为 hard。

### A2. 软隔离（soft, host + bridge 联网，tmeoa 网关）— 4 个

| task_id | 类别 | eval_metric（实测 primary） | L1 结果 | 说明 |
| --- | --- | --- | :---: | --- |
| `claudini.random` | adversarial | asr=0.0 | PASS | 真实拒答（TmeoaError 机制保证非空值）|
| `claudini.injection` | adversarial | asr≈0.33 | PASS | 小预算单次测量 |
| `claudini.safeguard` | adversarial | asr=0.0 | PASS | 真实拒答 |
| `ara.understanding` | agent_eval | abs_correctness_success_rate=0.4 | PASS | soft/联网 |

**Tier A 小结：15 个任务可直接投入双循环 / 研究循环，产出经验证的实验信号。**
注：`autolab.safety_router` 目录 `eval_metric` 字段标的是 `total_params`，但 runner 实测主指标为 `accuracy=0.6719`（已 L1 验证），字段命名与实测存在不一致，不影响可用性。

---

## 二、Tier A' —— 平台原生 / 自定义（enabled，走平台生产路径）⚠️ 共 9 个，非本次 OSS L1 范围

这些任务由平台自身机制保障可运行，但**不在本次 OSS L1 验证活动内**，未做单任务 L1 复跑确认：

| 分组 | task_id（5+4） | harness | 状态 |
| --- | --- | --- | --- |
| 平台原生 (kaggle_eval) | `platform.breast_cancer` `platform.iris` `platform.spaceship` `platform.titanic` `platform.wine` | kaggle_eval（双循环 + 5 折 CV）| enabled，平台生产路径可跑 |
| 用户自定义 (sandbox) | `custom.4_2`(图像) `custom.4_3`(音频) `custom.embedding_2`(嵌入) `custom.task_20260811171209`(文本) | `*_cls_sandbox` | enabled，upload 即跑 |

> 另有 `benchmark_tasks/` 下 **17 个扫描任务**（registry 注册，8 类型）：据既有记忆**仅 `tabular_classification` 实跑，其余 tracked-only**（不在 `get_catalog()` 内）。

**是否计入「完整实验验证条件」**：平台原生 5 个走的是平台生产 eval 路径、设计上即具备验证条件；自定义 4 个由上传数据 + sandbox runner 保障。若按「平台级可验证」口径，可算入；若严格按「本次 OSS L1 实跑确认」口径，则不算（归入待确认）。

---

## 三、Tier B —— 可 launch 但 env-blocked（不具备完整验证条件）⚠️ 共 19 个

SAB 轻量子集（除 `#92` 外的 19 个）。沙箱镜像仅含 `numpy/pandas/scikit-learn/scipy`，
这些任务的 gold/eval 程序依赖 `rdkit / neurokit2 / biopsykit / ccobra / matminer / geopandas / MDAnalysis / cftime+iris`，
在裸镜像 `import` 即失败。**launch 仍诚实完成**（SUCCEEDED + result.json + EvalCompletedEvent，`passed=False`，`port_notes` 点名缺失模块）——非缺陷，但**不能产出有意义的指标**，故不具备完整验证条件。

`sab.bio_eventrelated_analyze_29` `sab.cft_37` `sab.cogsci_pattern_high_sim_67` `sab.deforestation_21`
`sab.dili_models_ecfp_rf_18` `sab.dili_models_ecfp_svm_19` `sab.hrv_analyze_34` `sab.imu_44`
`sab.ligand_fingerprint_26` `sab.mat_feature_select_2` `sab.md_knn_41` `sab.md_rf_40`
`sab.nvc_accuracies_60` `sab.nvc_gen_ind_58` `sab.polynomial_fit_87` `sab.predict_bulk_modulus_3`
`sab.questionnaire_45` `sab.rrv_analyze_35` `sab.saliva_85`

⚠️ 注：上述 19 条即 SAB-19 文件全量；`sab.h_importances_92` 已升入 Tier A。翻为 PASS 需重建带 domain lib 的镜像（与离线 `--network none` 哲学有张力）。

---

## 四、Tier D —— 禁用 / 灰掉（不具备验证条件）共 3 个

| task_id | 原因 |
| --- | --- |
| `mlevolve.mle_bench` | **用户显式排除**（外部 openai/mle-bench，本仓未打包），`enabled=False` |
| `suite.mle_bench` | 外部 suite manifest，`enabled=False` |
| `suite.science_agent_bench` | 外部 suite manifest，`enabled=False` |

---

## 五、汇总

| 分层 | 数量 | 是否「具备完整实验验证条件」 |
| --- | :---: | :---: |
| **Tier A（OSS L1 已验证）** | **15** | ✅ 是（直接答案） |
| Tier A'（平台原生 5 + 自定义 4） | 9 | ⚠️ 平台级可验证，未做本次 L1 复跑 |
| Tier B（SAB env-blocked） | 19 | ❌ 否（launch 诚实但无有效指标） |
| Tier D（禁用/灰掉） | 3 | ❌ 否 |
| 扫描任务（17，仅 1 实跑） | 17 | ⚠️ 仅 `tabular_classification` 实跑 |
| **目录总计（get_catalog）** | **46** | — |

### 直接回答
**当前「具备完整实验验证条件」的 benchmark 任务 = Tier A 的 15 个 OSS 任务**
（11 硬隔离 + 4 软隔离；含 `autolab`×7、`arbor`×1、`autoclaude`×1、`claudini`×3、`ara`×1、`arc_bench`×1、`sab`×1）。
若把平台生产路径也计入，则另加平台原生 5 + 自定义 4 = 9 个；其余 19 个 SAB 因缺 domain lib 暂不具备，3 个被禁用。
