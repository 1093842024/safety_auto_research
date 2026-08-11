"""Custom research-task registration: schema-driven task-type specs + persistence.

This module is the single source of truth for *what a researcher must fill in /
upload* when registering a new research task in the console. The frontend
renders its registration form dynamically from ``get_task_type_specs()`` so the
two sides can never drift apart.

Design notes
------------
* 8 task types are supported (tabular / text / image / audio classification,
  LLM/MLLM SFT, LLM/MLLM RL, LLM/MLLM on-policy distillation, multimodal
  embedding via contrastive / self-supervised learning).
* Only ``tabular_classification`` is *platform-executable* today: it maps onto
  the generic ``kaggle_eval`` path (train.csv + target column + CV + threshold)
  and therefore drives the dual loop end-to-end. Every other type is registered
  as a *tracked* task (config recorded, run command suggested, executed by an
  external harness).
* Registered tasks persist to a JSON file (``data/custom_tasks.json`` by
  default, override with env ``CUSTOM_TASKS_STORE``) and are merged into the
  benchmark catalog by ``benchmark_tasks.get_catalog()``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import time
from typing import Any

_LOCK = threading.Lock()


@contextlib.contextmanager
def _cross_process_lock() -> Any:
    """Advisory cross-process mutex on a sidecar ``.lock`` file (H1 fix).

    ``threading.Lock`` only serializes threads *within one process*. With multiple
    worker processes (e.g. ``uvicorn --workers N``) two processes could rewrite the
    custom-tasks JSON concurrently, causing lost updates or (mid-write) corruption.
    A ``flock`` on a sidecar lock file gives the cross-process exclusion the store
    needs. The lock is released automatically on process exit.
    """

    path = _store_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _store_path() -> str:
    return os.environ.get(
        "CUSTOM_TASKS_STORE", os.path.join(_PKG_ROOT, "data", "custom_tasks.json")
    )


# --------------------------------------------------------------------------- #
# Field-spec vocabulary                                                       #
#   type: text | number | select | multiselect | textarea | tags | bool |     #
#         path | upload                                                       #
#   group: basic | data | model | train | eval                                #
# --------------------------------------------------------------------------- #

def _f(
    key: str,
    label: str,
    ftype: str,
    group: str,
    *,
    required: bool = False,
    default: Any = None,
    options: list[dict[str, str]] | None = None,
    help: str = "",
    placeholder: str = "",
    min: float | None = None,
    max: float | None = None,
    step: float | None = None,
    accept: str = "",
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": ftype,
        "group": group,
        "required": required,
    }
    if default is not None:
        d["default"] = default
    if options:
        d["options"] = options
    if help:
        d["help"] = help
    if placeholder:
        d["placeholder"] = placeholder
    if min is not None:
        d["min"] = min
    if max is not None:
        d["max"] = max
    if step is not None:
        d["step"] = step
    if accept:
        d["accept"] = accept
    return d


def _opt(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"value": v, "label": l} for v, l in pairs]


# Fields shared by every task type (rendered first, group=basic / eval).
COMMON_FIELDS: list[dict[str, Any]] = [
    _f("name", "任务名称", "text", "basic", required=True,
       placeholder="如：中文新闻文本分类 v1"),
    _f("description", "任务描述 / 研究目标", "textarea", "basic",
       placeholder="一句话说明数据来源、研究目标与成功标准。"),
    _f("tags", "标签", "tags", "basic", help="用于目录检索，如 nlp、classification。"),
    _f("eval_metric", "评测指标名", "text", "eval", required=True,
       placeholder="如 accuracy / f1_macro / recall@10"),
    _f("direction", "指标方向", "select", "eval", required=True, default="higher",
       options=_opt(("higher", "越高越好 ↑"), ("lower", "越低越好 ↓"))),
    _f("baseline", "基线值（可空）", "number", "eval", step=0.0001,
       help="当前已知基线成绩；留空表示暂无。"),
    _f("target_value", "目标值 / 通过门限（可空）", "number", "eval", step=0.0001,
       help="达到该值即视为研究目标达成，将写入任务 gates。"),
]


TASK_TYPE_SPECS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ #
    {
        "type_id": "tabular_classification",
        "label": "表格分类（平台可实跑）",
        "icon": "📊",
        "modality": "tabular",
        "harness": "kaggle_eval",
        "executable": True,
        "summary": "CSV 表格 + 目标列的分类任务，直接驱动双循环（内循环 sklearn 评测 → 外循环审计）。",
        "default_metric": {"eval_metric": "cv_accuracy", "direction": "higher"},
        "fields": [
            _f("data_dir", "数据目录（须含 train.csv）", "path", "data", required=True,
               placeholder="/path/to/dataset（内含 train.csv）",
               help="也可先用下方上传框上传 csv/zip，系统会自动填入路径。"),
            _f("dataset_upload", "上传数据（csv 或 zip，可选）", "upload", "data",
               accept=".csv,.zip", help="上传后自动写入上方数据目录。"),
            _f("target_col", "目标列（label 列名）", "text", "data", required=True,
               placeholder="如 Survived / label"),
            _f("id_col", "ID 列（可空，将被剔除）", "text", "data", placeholder="如 PassengerId"),
            _f("drop_cols", "额外剔除列", "tags", "data", help="不参与建模的列名。"),
            _f("model", "基线模型", "select", "model", default="gbm",
               options=_opt(("gbm", "gbm（梯度提升）"), ("gbm-strong", "gbm-strong（HistGB）"),
                            ("rf", "rf（随机森林）"), ("logreg", "logreg（逻辑回归）"))),
            _f("cv_folds", "交叉验证折数", "number", "train", default=5, min=2, max=20, step=1),
            _f("threshold", "内循环通过门限", "number", "train", default=0.8, min=0, max=1, step=0.01,
               help="内循环 CV 指标达到该值才算通过。"),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "text_classification",
        "label": "文本分类",
        "icon": "📝",
        "modality": "text",
        "harness": "manual",
        "executable": False,
        "summary": "csv/jsonl 文本 + 标签的分类任务（BERT/RoBERTa 微调或 TF-IDF 基线），平台记录设定与目标，外部执行。",
        "default_metric": {"eval_metric": "f1_macro", "direction": "higher"},
        "fields": [
            _f("dataset_path", "数据文件（csv/jsonl）", "path", "data", required=True,
               placeholder="/path/to/train.jsonl"),
            _f("dataset_upload", "上传数据（可选）", "upload", "data", accept=".csv,.jsonl,.json,.zip"),
            _f("text_col", "文本字段名", "text", "data", required=True, default="text"),
            _f("label_col", "标签字段名", "text", "data", required=True, default="label"),
            _f("num_classes", "类别数", "number", "data", required=True, min=2, step=1),
            _f("language", "语言", "select", "data", default="zh",
               options=_opt(("zh", "中文"), ("en", "英文"), ("multi", "多语言"))),
            _f("base_model", "基座模型", "select", "model", default="hfl/chinese-roberta-wwm-ext",
               options=_opt(("hfl/chinese-roberta-wwm-ext", "chinese-roberta-wwm-ext"),
                            ("bert-base-chinese", "bert-base-chinese"),
                            ("roberta-base", "roberta-base（英文）"),
                            ("tfidf-linear", "TF-IDF + 线性模型（无 GPU 基线）"))),
            _f("max_seq_len", "最大序列长度", "number", "train", default=256, min=16, max=8192, step=1),
            _f("learning_rate", "学习率", "number", "train", default=2e-5, step=1e-6),
            _f("num_epochs", "训练轮数", "number", "train", default=3, min=1, max=100, step=1),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "image_classification",
        "label": "图像分类",
        "icon": "🖼️",
        "modality": "image",
        "harness": "manual",
        "executable": False,
        "summary": "ImageFolder 结构（train/<class>/*.jpg）的图像分类，torchvision/timm backbone 微调。",
        "default_metric": {"eval_metric": "top1_accuracy", "direction": "higher"},
        "fields": [
            _f("data_dir", "数据根目录（ImageFolder 结构）", "path", "data", required=True,
               placeholder="/path/to/images，内含 train/<类名>/*.jpg",
               help="标准 ImageFolder：每个类别一个子目录；如有 val/ 目录将用于验证。"),
            _f("num_classes", "类别数", "number", "data", required=True, min=2, step=1),
            _f("val_split", "验证集比例（无 val/ 目录时）", "number", "data", default=0.1, min=0.05, max=0.5, step=0.05),
            _f("image_size", "输入图像尺寸", "number", "model", default=224, min=32, max=1024, step=1),
            _f("base_model", "Backbone", "select", "model", default="resnet50",
               options=_opt(("resnet50", "ResNet-50"), ("vit_b_16", "ViT-B/16"),
                            ("efficientnet_b0", "EfficientNet-B0"), ("convnext_tiny", "ConvNeXt-Tiny"))),
            _f("pretrained", "使用 ImageNet 预训练权重", "bool", "model", default=True),
            _f("augmentation", "数据增广", "select", "train", default="basic",
               options=_opt(("none", "无"), ("basic", "基础（翻转/裁剪）"), ("randaug", "RandAugment"))),
            _f("learning_rate", "学习率", "number", "train", default=1e-3, step=1e-5),
            _f("num_epochs", "训练轮数", "number", "train", default=20, min=1, max=300, step=1),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "audio_classification",
        "label": "音频分类",
        "icon": "🎧",
        "modality": "audio",
        "harness": "manual",
        "executable": False,
        "summary": "音频目录 + manifest（filepath,label）的分类任务，logmel/MFCC 特征或 wav2vec2 类预训练模型。",
        "default_metric": {"eval_metric": "accuracy", "direction": "higher"},
        "fields": [
            _f("data_dir", "音频根目录", "path", "data", required=True, placeholder="/path/to/audio"),
            _f("manifest_path", "manifest 文件（csv：filepath,label）", "path", "data", required=True,
               placeholder="/path/to/manifest.csv"),
            _f("num_classes", "类别数", "number", "data", required=True, min=2, step=1),
            _f("sample_rate", "采样率", "select", "data", default="16000",
               options=_opt(("16000", "16 kHz"), ("22050", "22.05 kHz"), ("44100", "44.1 kHz"))),
            _f("clip_seconds", "切片时长（秒）", "number", "data", default=5, min=1, max=60, step=1),
            _f("feature", "输入特征", "select", "model", default="logmel",
               options=_opt(("logmel", "log-Mel 频谱"), ("mfcc", "MFCC"),
                            ("waveform", "原始波形（端到端预训练模型）"))),
            _f("base_model", "基座模型", "select", "model", default="panns_cnn14",
               options=_opt(("panns_cnn14", "PANNs CNN14"), ("ast", "AST（Audio Spectrogram Transformer)"),
                            ("wav2vec2-base", "wav2vec2-base"), ("hubert-base", "HuBERT-base"))),
            _f("learning_rate", "学习率", "number", "train", default=1e-4, step=1e-6),
            _f("num_epochs", "训练轮数", "number", "train", default=30, min=1, max=300, step=1),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "llm_sft",
        "label": "大模型 / 多模态大模型 SFT",
        "icon": "🧠",
        "modality": "llm",
        "harness": "manual",
        "executable": False,
        "summary": "监督微调（full/LoRA/QLoRA）；支持纯文本与图文/音频多模态指令数据（messages/alpaca/sharegpt 格式）。",
        "default_metric": {"eval_metric": "eval_loss", "direction": "lower"},
        "fields": [
            _f("llm_modality", "模态", "select", "model", required=True, default="text",
               options=_opt(("text", "纯文本 LLM"), ("vision-language", "图文多模态（VLM）"),
                            ("audio-language", "语音多模态"), ("omni", "全模态"))),
            _f("base_model", "基座模型", "text", "model", required=True,
               placeholder="如 Qwen3-8B / Qwen2.5-VL-7B-Instruct"),
            _f("dataset_path", "SFT 数据集（jsonl）", "path", "data", required=True,
               placeholder="/path/to/sft.jsonl",
               help="多模态数据需在样本中携带 images/audios 字段（本地路径或 URL）。"),
            _f("dataset_upload", "上传数据（可选）", "upload", "data", accept=".jsonl,.json,.zip"),
            _f("dataset_format", "数据格式", "select", "data", required=True, default="messages",
               options=_opt(("messages", "messages（OpenAI 会话格式）"),
                            ("alpaca", "alpaca（instruction/input/output）"),
                            ("sharegpt", "sharegpt（conversations）"))),
            _f("train_type", "微调方式", "select", "train", required=True, default="lora",
               options=_opt(("full", "全参数"), ("lora", "LoRA"), ("qlora", "QLoRA（量化+LoRA）"))),
            _f("lora_rank", "LoRA rank（lora/qlora 时）", "number", "train", default=16, min=1, max=512, step=1),
            _f("learning_rate", "学习率", "number", "train", default=1e-4, step=1e-6),
            _f("num_epochs", "训练轮数", "number", "train", default=2, min=1, max=50, step=1),
            _f("max_length", "最大序列长度", "number", "train", default=4096, min=256, max=262144, step=1),
            _f("per_device_batch_size", "单卡 batch size", "number", "train", default=1, min=1, max=512, step=1),
            _f("gradient_accumulation", "梯度累积步数", "number", "train", default=8, min=1, max=1024, step=1),
            _f("eval_benchmark", "评测基准 / 验证集", "text", "eval",
               placeholder="如 CEval / MMBench / 自建验证集路径",
               help="SFT 后用什么衡量效果；默认跟踪 eval_loss。"),
            _f("eval_dataset_path", "自动化评估数据集（jsonl，可选）", "path", "eval",
               placeholder="/path/to/eval.jsonl（含 prompt/reference，用于 LLM-as-judge 评测）",
               help="填写后可在运行结束后触发自动评估，按准召率入榜。"),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "llm_rl",
        "label": "大模型 / 多模态大模型 RL",
        "icon": "🎯",
        "modality": "llm",
        "harness": "manual",
        "executable": False,
        "summary": "强化学习后训练（GRPO/PPO/DPO/KTO 等）；需指定奖励来源（规则验证器 / 奖励模型 / 偏好对）。",
        "default_metric": {"eval_metric": "benchmark_accuracy", "direction": "higher"},
        "fields": [
            _f("algorithm", "RL 算法", "select", "train", required=True, default="grpo",
               options=_opt(("grpo", "GRPO（组相对策略优化）"), ("ppo", "PPO"),
                            ("dpo", "DPO（偏好优化，离线）"), ("kto", "KTO"), ("rloo", "RLOO"))),
            _f("llm_modality", "模态", "select", "model", required=True, default="text",
               options=_opt(("text", "纯文本 LLM"), ("vision-language", "图文多模态（VLM）"),
                            ("audio-language", "语音多模态"))),
            _f("base_model", "基座模型（通常为 SFT 后 checkpoint）", "text", "model", required=True,
               placeholder="如 Qwen3-8B-SFT / checkpoints/sft-final"),
            _f("prompt_dataset", "Prompt / 偏好数据集", "path", "data", required=True,
               placeholder="/path/to/prompts.jsonl",
               help="GRPO/PPO 需 prompt 集；DPO/KTO 需偏好对（chosen/rejected）。"),
            _f("dataset_upload", "上传数据（可选）", "upload", "data", accept=".jsonl,.json,.zip"),
            _f("reward_type", "奖励来源", "select", "train", required=True, default="rule_verifier",
               options=_opt(("rule_verifier", "规则验证器（数学/代码可验证任务）"),
                            ("reward_model", "奖励模型（RM 打分）"),
                            ("preference_pairs", "偏好对（DPO/KTO 离线数据）"))),
            _f("reward_model_path", "奖励模型路径（reward_model 时必填）", "text", "train",
               placeholder="如 Skywork/Skywork-Reward-Llama-3.1-8B"),
            _f("kl_coef", "KL 系数", "number", "train", default=0.001, step=0.0001),
            _f("num_generations", "每 prompt 采样数（GRPO group size）", "number", "train",
               default=8, min=2, max=64, step=1),
            _f("max_completion_length", "最大生成长度", "number", "train", default=4096,
               min=128, max=131072, step=1),
            _f("temperature", "采样温度", "number", "train", default=1.0, min=0, max=2, step=0.05),
            _f("learning_rate", "学习率", "number", "train", default=1e-6, step=1e-7),
            _f("eval_benchmark", "评测基准", "text", "eval", required=True,
               placeholder="如 AIME24 / GSM8K / MathVista"),
            _f("eval_dataset_path", "自动化评估数据集（jsonl，可选）", "path", "eval",
               placeholder="/path/to/eval.jsonl（含 prompt/reference，用于 LLM-as-judge 评测）",
               help="填写后可在运行结束后触发自动评估，按准召率入榜。"),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "llm_opd",
        "label": "大模型 / 多模态大模型 OPD（在线策略蒸馏）",
        "icon": "🧪",
        "modality": "llm",
        "harness": "manual",
        "executable": False,
        "summary": "On-Policy Distillation：学生模型自采样，教师模型逐 token 打分（reverse-KL 密集监督），"
                   "约 1/10 RL 成本达到相近效果（Thinking Machines 2025 / MS-SWIFT GKD 实现）。",
        "default_metric": {"eval_metric": "benchmark_accuracy", "direction": "higher"},
        "fields": [
            _f("student_model", "学生模型", "text", "model", required=True,
               placeholder="如 Qwen3-8B-Base"),
            _f("teacher_model", "教师模型", "text", "model", required=True,
               placeholder="如 Qwen3-32B（只推理打分，不更新梯度）"),
            _f("llm_modality", "模态", "select", "model", default="text",
               options=_opt(("text", "纯文本 LLM"), ("vision-language", "图文多模态（VLM）"))),
            _f("prompt_dataset", "Prompt 数据集（无需标注答案）", "path", "data", required=True,
               placeholder="/path/to/prompts.jsonl",
               help="OPD 只需 prompt：学生自己生成轨迹，教师逐 token 评分。"),
            _f("dataset_upload", "上传数据（可选）", "upload", "data", accept=".jsonl,.json,.zip"),
            _f("distill_loss", "蒸馏损失", "select", "train", default="reverse_kl",
               options=_opt(("reverse_kl", "Reverse KL（TML 推荐）"),
                            ("gkd_jsd", "GKD 广义 JSD（beta 插值）"))),
            _f("lmbda", "lmbda（学生自采样比例）", "number", "train", default=1.0, min=0, max=1, step=0.05,
               help="1.0=完全 on-policy（学生自采样）；0=退化为 off-policy 序列蒸馏。"),
            _f("beta", "beta（JSD 插值系数）", "number", "train", default=1.0, min=0, max=1, step=0.05,
               help="1.0 即 reverse KL。"),
            _f("max_completion_length", "最大生成长度", "number", "train", default=8192,
               min=128, max=131072, step=1),
            _f("learning_rate", "学习率", "number", "train", default=1e-5, step=1e-6),
            _f("use_vllm", "vLLM 加速学生采样", "bool", "train", default=True),
            _f("teacher_deepspeed", "教师侧 DeepSpeed 等级", "select", "train", default="zero3",
               options=_opt(("zero2", "ZeRO-2"), ("zero3", "ZeRO-3（大教师推荐）"))),
            _f("eval_benchmark", "评测基准", "text", "eval", required=True,
               placeholder="如 AIME24 / GSM8K / IF-Eval"),
            _f("eval_dataset_path", "自动化评估数据集（jsonl，可选）", "path", "eval",
               placeholder="/path/to/eval.jsonl（含 prompt/reference，用于 LLM-as-judge 评测）",
               help="填写后可在运行结束后触发自动评估，按准召率入榜。"),
        ],
    },
    # ------------------------------------------------------------------ #
    {
        "type_id": "embedding_contrastive",
        "label": "图文音 Embedding（对比学习 / 自监督）",
        "icon": "🧲",
        "modality": "multimodal",
        "harness": "manual",
        "executable": False,
        "summary": "对比学习 / 自监督表征模型（SimCSE/CLIP/SimCLR/MoCo 等），按模态组合与数据格式注册，"
                   "以检索/STS/零样本分类作为评测协议。",
        "default_metric": {"eval_metric": "recall_at_10", "direction": "higher"},
        "fields": [
            _f("modality_pair", "模态组合", "select", "model", required=True, default="text-image",
               options=_opt(("text-text", "文-文（SimCSE/句向量）"),
                            ("text-image", "图-文（CLIP 式）"),
                            ("text-audio", "音-文（CLAP 式）"),
                            ("image-image", "图-图（SimCLR/MoCo 自监督）"),
                            ("audio-audio", "音-音（自监督）"))),
            _f("method", "训练方法", "select", "model", required=True, default="clip",
               options=_opt(("clip", "CLIP（双塔图文对比）"), ("siglip", "SigLIP（sigmoid 损失）"),
                            ("simcse", "SimCSE（dropout 正例）"), ("simclr", "SimCLR（增广正例）"),
                            ("moco", "MoCo（动量队列）"), ("byol", "BYOL（无负样本）"))),
            _f("data_format", "数据格式", "select", "data", required=True, default="pairs",
               options=_opt(("pairs", "成对样本（anchor,positive）"),
                            ("triplets", "三元组（anchor,positive,negative）"),
                            ("image_text_pairs", "图文对 jsonl（image_path,text）"),
                            ("single_view", "单视图（自监督，训练时增广出正例）"))),
            _f("dataset_path", "数据集路径", "path", "data", required=True,
               placeholder="/path/to/pairs.jsonl 或数据目录"),
            _f("dataset_upload", "上传数据（可选）", "upload", "data", accept=".jsonl,.json,.csv,.zip"),
            _f("text_encoder", "文本编码器（含文本模态时）", "text", "model",
               placeholder="如 bge-base-zh-v1.5 / bert-base-chinese"),
            _f("vision_encoder", "视觉编码器（含图像模态时）", "text", "model",
               placeholder="如 ViT-B/16 / resnet50"),
            _f("audio_encoder", "音频编码器（含音频模态时）", "text", "model",
               placeholder="如 HTSAT / wav2vec2-base"),
            _f("embedding_dim", "向量维度", "number", "model", default=768, min=32, max=8192, step=1),
            _f("temperature", "对比温度 τ", "number", "train", default=0.07, min=0.001, max=1, step=0.001),
            _f("batch_size", "batch size（决定 in-batch 负样本数）", "number", "train",
               default=256, min=8, max=65536, step=1),
            _f("num_epochs", "训练轮数", "number", "train", default=10, min=1, max=300, step=1),
            _f("eval_protocol", "评测协议", "select", "eval", required=True, default="retrieval_recall",
               options=_opt(("retrieval_recall", "跨模态检索 Recall@K"),
                            ("sts_spearman", "STS 语义相似度（Spearman）"),
                            ("zeroshot_classification", "零样本分类准确率"),
                            ("linear_probe", "线性探针准确率"))),
            _f("eval_dataset_path", "自动化检索评测集（jsonl，可选）", "path", "eval",
               placeholder="/path/to/retrieval.jsonl（含 query/positive/[negatives]）",
               help="填写后可在运行结束后触发检索评测，按 Recall@K 入榜。"),
        ],
    },
]

_SPEC_BY_ID = {s["type_id"]: s for s in TASK_TYPE_SPECS}


def get_task_type_specs() -> list[dict[str, Any]]:
    """Full form schema for the frontend: per-type fields prefixed by COMMON_FIELDS."""
    out = []
    for s in TASK_TYPE_SPECS:
        merged = dict(s)
        merged["common_fields"] = COMMON_FIELDS
        out.append(merged)
    return out


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

# 每类任务的合法评测指标白名单（小写 token；recall@K 与 recall_at_K 视为等价）。
_VALID_METRICS: dict[str, set[str]] = {
    "tabular_classification": {
        "accuracy", "acc", "cv_accuracy", "f1", "f1_macro", "f1_micro", "f1_weighted",
        "precision", "recall", "auc", "roc_auc", "balanced_accuracy",
    },
    "text_classification": {
        "accuracy", "acc", "f1", "f1_macro", "f1_micro", "f1_weighted",
        "precision", "recall", "auc", "roc_auc", "balanced_accuracy",
    },
    "image_classification": {
        "accuracy", "acc", "top1_accuracy", "top5_accuracy", "f1_macro",
        "precision", "recall", "auc",
    },
    "audio_classification": {
        "accuracy", "acc", "f1_macro", "precision", "recall", "auc",
    },
    "llm_sft": {
        "eval_loss", "loss", "perplexity", "ppl", "token_accuracy", "accuracy", "acc",
    },
    "llm_rl": {
        "reward", "reward_score", "win_rate", "accuracy", "acc",
        "kl", "benchmark_accuracy", "auc",
    },
    "llm_opd": {
        "reward", "reward_score", "win_rate", "accuracy", "acc",
        "benchmark_accuracy", "kl",
    },
    "embedding_contrastive": {
        "recall_at_1", "recall_at_5", "recall_at_10",
        "recall@1", "recall@5", "recall@10",
        "ndcg", "contrastive_accuracy", "retrieval_acc",
        "spearman", "sts_spearman", "accuracy", "acc",
    },
}

# 指标默认方向（用于校验用户填写的 direction 是否自相矛盾）。
_METRIC_DIR: dict[str, str] = {
    "loss": "lower", "eval_loss": "lower", "perplexity": "lower", "ppl": "lower",
    "mse": "lower", "rmse": "lower", "mae": "lower", "kl": "lower", "kld": "lower",
    "accuracy": "higher", "acc": "higher", "cv_accuracy": "higher",
    "top1_accuracy": "higher", "top5_accuracy": "higher",
    "f1": "higher", "f1_macro": "higher", "f1_micro": "higher", "f1_weighted": "higher",
    "precision": "higher", "recall": "higher", "auc": "higher", "roc_auc": "higher",
    "balanced_accuracy": "higher",
    "recall_at_1": "higher", "recall_at_5": "higher", "recall_at_10": "higher",
    "recall@1": "higher", "recall@5": "higher", "recall@10": "higher",
    "ndcg": "higher", "reward": "higher", "reward_score": "higher", "win_rate": "higher",
    "benchmark_accuracy": "higher", "contrastive_accuracy": "higher",
    "retrieval_acc": "higher", "spearman": "higher", "sts_spearman": "higher",
    "token_accuracy": "higher",
}

_DATASET_EXTS = {
    "text": {".csv", ".jsonl", ".json"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"},
    "audio": {".wav", ".mp3", ".flac", ".m4a", ".ogg"},
}


def _norm_metric(m: str) -> str:
    return str(m or "").strip().lower().replace("@", "_at_")


def _metric_direction(metric: str) -> str | None:
    """指标默认优化方向（higher/lower）。未知指标按命名后缀推断。"""
    if metric in _METRIC_DIR:
        return _METRIC_DIR[metric]
    if metric.endswith(("_loss", "mse", "rmse", "mae", "kl", "kld", "ppl", "perplexity")):
        return "lower"
    if metric.endswith(
        ("_accuracy", "_acc", "_score", "_recall", "_f1", "_precision", "_auc",
         "_reward", "_rate", "_ndcg", "_spearman", "_mrr")
    ):
        return "higher"
    return None


def _metric_allowed(task_type: str, metric: str) -> bool:
    allowed = _VALID_METRICS.get(task_type, set())
    if metric in allowed:
        return True
    # LLM 类任务的评测往往就是某个 benchmark 的 *_accuracy / *_score，从宽。
    if task_type.startswith("llm_"):
        if metric.endswith(("_accuracy", "_acc", "_score")):
            return True
    # embedding 的 recall@K 任意 K 均合法。
    if task_type == "embedding_contrastive" and (
        metric.startswith("recall_at_") or metric.startswith("recall@")
    ):
        return True
    return False


def _check_metric_match(task_type: str, values: dict[str, Any], errors: list[str]) -> None:
    """评测指标白名单 + direction 自洽性校验。"""
    raw = str(values.get("eval_metric") or "").strip()
    if not raw:
        return  # 必填已由通用逻辑处理
    metric = _norm_metric(raw)
    if not _metric_allowed(task_type, metric):
        allowed = sorted(_VALID_METRICS.get(task_type, set()))
        errors.append(
            f"评测指标「{raw}」不适合「{_SPEC_BY_ID[task_type]['label']}」；"
            f"该任务类型合法指标候选: {allowed}"
        )
        return
    user_dir = str(values.get("direction") or "").strip().lower()
    known = _metric_direction(metric)
    if known and user_dir and user_dir != known:
        errors.append(
            f"指标「{raw}」默认方向为「{'越高越好 ↑' if known == 'higher' else '越低越好 ↓'}」"
            f"，但您填写的方向是「{'越高越好 ↑' if user_dir == 'higher' else '越低越好 ↓'}」，请核对。"
        )


def _check_dataset_format(task_type: str, values: dict[str, Any], errors: list[str]) -> None:
    """按任务类型做数据集格式 / 结构校验（路径存在时才深入检查，避免阻断远端路径注册）。"""

    def _ext(p: str) -> str:
        return os.path.splitext(str(p or ""))[1].lower()

    def _has_files(d: str, exts: set[str]) -> bool:
        d = str(d or "").strip()
        if not d or not os.path.isdir(d):
            return False
        for _, _, files in os.walk(d):
            if any(f.lower().endswith(tuple(exts)) for f in files):
                return True
        return False

    if task_type == "tabular_classification":
        data_dir = str(values.get("data_dir") or "").strip()
        if data_dir and os.path.isdir(data_dir):
            train = os.path.join(data_dir, "train.csv")
            if os.path.exists(train):
                tcol = str(values.get("target_col") or "").strip()
                if tcol:
                    try:
                        import csv

                        with open(train, newline="", encoding="utf-8", errors="ignore") as fh:
                            header = next(csv.reader(fh), [])
                        if tcol not in header:
                            errors.append(
                                f"target_col「{tcol}」不在 train.csv 表头中；表头前若干列为: {header[:12]}"
                            )
                    except Exception:
                        pass
        elif data_dir and _ext(data_dir) not in {".csv", ".zip", ""}:
            errors.append("表格分类数据目录应为包含 train.csv 的目录（或上传 zip）。")

    elif task_type == "text_classification":
        p = str(values.get("dataset_path") or "").strip()
        if p:
            if os.path.exists(p) and os.path.isfile(p) and _ext(p) not in _DATASET_EXTS["text"]:
                errors.append("文本分类数据集应为 csv/jsonl/json 文件")
            elif not os.path.exists(p) and _ext(p) and _ext(p) not in _DATASET_EXTS["text"]:
                errors.append("文本分类数据集扩展名应为 .csv/.jsonl/.json")

    elif task_type == "image_classification":
        d = str(values.get("data_dir") or "").strip()
        if d and os.path.isdir(d) and not _has_files(d, _DATASET_EXTS["image"]):
            errors.append("图像分类数据目录中未找到任何图片文件（jpg/png/webp/...）")
        elif d and not os.path.isdir(d) and _ext(d) not in _DATASET_EXTS["image"]:
            errors.append("图像分类数据目录扩展名应为图片格式（jpg/png/webp/...）")

    elif task_type == "audio_classification":
        d = str(values.get("data_dir") or "").strip()
        m = str(values.get("manifest_path") or "").strip()
        if d and os.path.isdir(d):
            if not _has_files(d, _DATASET_EXTS["audio"]) and not (m and os.path.exists(m)):
                errors.append("音频分类数据目录中未找到音频文件，且未提供有效 manifest")
        elif d and not os.path.isdir(d) and _ext(d) not in _DATASET_EXTS["audio"]:
            errors.append("音频分类数据应为音频目录或 manifest csv")

    elif task_type in ("llm_sft", "llm_rl", "llm_opd"):
        fmt = str(values.get("dataset_format") or "").strip().lower()
        if fmt and fmt not in ("messages", "alpaca", "sharegpt", "json", "jsonl", "csv"):
            errors.append(f"数据格式「{fmt}」不支持（可选 messages/alpaca/sharegpt）")
        tt = str(values.get("train_type") or "").strip().lower()
        if tt and tt not in ("full", "lora", "qlora"):
            errors.append(f"微调方式「{tt}」不支持（可选 full/lora/qlora）")

    elif task_type == "embedding_contrastive":
        pair = str(values.get("modality_pair") or "").strip().lower()
        valid_pairs = {"text-text", "text-image", "text-audio", "image-image", "audio-audio"}
        if pair and pair not in valid_pairs:
            errors.append(f"模态组合「{pair}」不支持（可选 {sorted(valid_pairs)}）")
        method = str(values.get("method") or "").strip().lower()
        if method and method not in ("clip", "siglip", "simcse", "simclr", "moco", "byol"):
            errors.append(f"训练方法「{method}」不支持（可选 clip/siglip/simcse/simclr/moco/byol）")


def validate_registration(task_type: str, values: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors (empty list == valid)."""
    errors: list[str] = []
    spec = _SPEC_BY_ID.get(task_type)
    if spec is None:
        return [f"未知任务类型: {task_type}（可选: {', '.join(_SPEC_BY_ID)}）"]

    all_fields = COMMON_FIELDS + spec["fields"]
    for f in all_fields:
        key, ftype = f["key"], f["type"]
        if ftype == "upload":  # upload widgets resolve into their path field
            continue
        v = values.get(key)
        empty = v is None or (isinstance(v, str) and not v.strip()) or (
            isinstance(v, list) and not v
        )
        if f.get("required") and empty:
            errors.append(f"缺少必填字段: {f['label']}（{key}）")
            continue
        if empty:
            continue
        if ftype == "number":
            try:
                num = float(v)
            except (TypeError, ValueError):
                errors.append(f"字段 {f['label']}（{key}）应为数字，收到: {v!r}")
                continue
            if f.get("min") is not None and num < f["min"]:
                errors.append(f"字段 {f['label']}（{key}）不能小于 {f['min']}")
            if f.get("max") is not None and num > f["max"]:
                errors.append(f"字段 {f['label']}（{key}）不能大于 {f['max']}")
        elif ftype in ("select",):
            allowed = {o["value"] for o in f.get("options", [])}
            if allowed and str(v) not in allowed:
                errors.append(f"字段 {f['label']}（{key}）取值 {v!r} 不在可选范围 {sorted(allowed)}")

    # Cross-field rules.
    if task_type == "llm_rl":
        if values.get("reward_type") == "reward_model" and not str(
            values.get("reward_model_path") or ""
        ).strip():
            errors.append("奖励来源为「奖励模型」时必须填写 reward_model_path")
    if task_type == "embedding_contrastive":
        pair = str(values.get("modality_pair") or "")
        enc_ok = False
        if "text" in pair and str(values.get("text_encoder") or "").strip():
            enc_ok = True
        if "image" in pair and str(values.get("vision_encoder") or "").strip():
            enc_ok = True
        if "audio" in pair and str(values.get("audio_encoder") or "").strip():
            enc_ok = True
        if not enc_ok:
            errors.append("请至少为所选模态组合填写一个对应的编码器（text/vision/audio encoder）")
    if task_type == "tabular_classification":
        data_dir = str(values.get("data_dir") or "").strip()
        if data_dir and os.path.isdir(data_dir) and not os.path.exists(
            os.path.join(data_dir, "train.csv")
        ):
            errors.append(f"数据目录 {data_dir} 中未找到 train.csv")

    # ---- 指标与任务类型匹配 ----
    _check_metric_match(task_type, values, errors)
    # ---- 数据集格式按任务类型校验 ----
    _check_dataset_format(task_type, values, errors)
    return errors


# --------------------------------------------------------------------------- #
# Run-command suggestion (tracked-only types)                                 #
# --------------------------------------------------------------------------- #

def suggest_run_command(task_type: str, values: dict[str, Any]) -> str:
    g = lambda k, d="": str(values.get(k) or d)  # noqa: E731
    if task_type == "tabular_classification":
        return "POST /benchmark-tasks/{task_id}/launch  (runs dual loop via kaggle_eval)"
    if task_type == "text_classification":
        return (
            f"python train_text_cls.py --model {g('base_model')} --data {g('dataset_path')} "
            f"--text-col {g('text_col')} --label-col {g('label_col')} "
            f"--max-len {g('max_seq_len', '256')} --epochs {g('num_epochs', '3')}"
        )
    if task_type == "image_classification":
        return (
            f"python train_image_cls.py --arch {g('base_model')} --data {g('data_dir')} "
            f"--img-size {g('image_size', '224')} --epochs {g('num_epochs', '20')}"
        )
    if task_type == "audio_classification":
        return (
            f"python train_audio_cls.py --model {g('base_model')} --manifest {g('manifest_path')} "
            f"--sr {g('sample_rate', '16000')} --feature {g('feature', 'logmel')}"
        )
    if task_type == "llm_sft":
        return (
            f"swift sft --model {g('base_model')} --dataset {g('dataset_path')} "
            f"--train_type {g('train_type', 'lora')} --num_train_epochs {g('num_epochs', '2')} "
            f"--max_length {g('max_length', '4096')} --learning_rate {g('learning_rate', '1e-4')}"
        )
    if task_type == "llm_rl":
        return (
            f"swift rlhf --rlhf_type {g('algorithm', 'grpo')} --model {g('base_model')} "
            f"--dataset {g('prompt_dataset')} --num_generations {g('num_generations', '8')} "
            f"--max_completion_length {g('max_completion_length', '4096')}"
        )
    if task_type == "llm_opd":
        return (
            f"swift rlhf --rlhf_type gkd --model {g('student_model')} "
            f"--teacher_model {g('teacher_model')} --dataset {g('prompt_dataset')} "
            f"--lmbda {g('lmbda', '1.0')} --beta {g('beta', '1.0')} "
            f"--use_vllm {str(values.get('use_vllm', True)).lower()} "
            f"--teacher_deepspeed {g('teacher_deepspeed', 'zero3')}"
        )
    if task_type == "embedding_contrastive":
        return (
            f"python train_embedding.py --method {g('method', 'clip')} "
            f"--pair {g('modality_pair')} --data {g('dataset_path')} "
            f"--temperature {g('temperature', '0.07')} --batch-size {g('batch_size', '256')}"
        )
    return "manual"


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #

def _load_raw() -> list[dict[str, Any]]:
    path = _store_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        # R7 fix: a corrupt custom-tasks file used to be silently swallowed and the
        # caller proceeded with an EMPTY list, so ``register_custom_task`` then
        # overwrite-wrote the file and every previously registered task was lost with
        # no trace. Back up the corrupt file (mirrors control_plane/store.py) so the
        # data is recoverable, then start empty.
        import shutil as _shutil

        _bak = f"{path}.corrupt.{int(time.time())}"
        try:
            _shutil.move(path, _bak)
            logging.error(
                "Corrupt custom-task store at %s backed up to %s (%s); starting empty.",
                path, _bak, exc,
            )
        except OSError as _bak_exc:
            logging.error(
                "Corrupt custom-task store at %s (backup failed: %s): %s; starting empty.",
                path, _bak_exc, exc,
            )
        return []


def _save_raw(items: list[dict[str, Any]]) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _slugify(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()
    # Non-ASCII names (e.g. Chinese) collapse to empty; use a time-based slug so
    # the generated task_id is still unique and human-distinguishable.
    return s or time.strftime("task_%Y%m%d%H%M%S")


def register_custom_task(task_type: str, values: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist a custom task registration; returns the stored record.

    Raises ``ValueError`` with all validation errors joined when invalid.
    """
    errors = validate_registration(task_type, values)
    if errors:
        raise ValueError("；".join(errors))
    spec = _SPEC_BY_ID[task_type]

    with _cross_process_lock():
        with _LOCK:
            items = _load_raw()
            existing = {it.get("task_id") for it in items}
            base = f"custom.{_slugify(str(values.get('name', '')))}"
            task_id = base
            i = 2
            while task_id in existing:
                task_id = f"{base}_{i}"
                i += 1

            record = {
                "task_id": task_id,
                "task_type": task_type,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "values": values,
                "label": spec["label"],
                "executable": bool(spec["executable"]),
                "harness": spec["harness"],
                "modality": spec["modality"],
            }
            items.append(record)
            _save_raw(items)
            return record


def list_custom_tasks() -> list[dict[str, Any]]:
    with _cross_process_lock():
        with _LOCK:
            return _load_raw()


def delete_custom_task(task_id: str) -> bool:
    with _cross_process_lock():
        with _LOCK:
            items = _load_raw()
            kept = [it for it in items if it.get("task_id") != task_id]
            if len(kept) == len(items):
                return False
            _save_raw(kept)
            return True


def get_custom_task(task_id: str) -> dict[str, Any] | None:
    for it in list_custom_tasks():
        if it["task_id"] == task_id:
            return it
    return None


def custom_task_to_benchmark_dict(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored custom-task record into a BenchmarkTask-shaped payload."""
    v = record["values"]
    spec = _SPEC_BY_ID.get(record["task_type"], {})
    gates: dict[str, Any] = {}
    if v.get("target_value") is not None and str(v.get("target_value")) != "":
        gates["target"] = v["target_value"]
    if record["task_type"] == "tabular_classification" and v.get("threshold") is not None:
        gates["threshold"] = v["threshold"]

    dataset_bits = []
    for k in ("data_dir", "dataset_path", "prompt_dataset", "manifest_path"):
        if v.get(k):
            dataset_bits.append(str(v[k]))
    desc = str(v.get("description") or "").strip()
    dataset_desc = (desc + ("；数据: " + " | ".join(dataset_bits) if dataset_bits else "")).strip("；") or spec.get("summary", "")

    baseline = v.get("baseline")
    try:
        baseline = float(baseline) if baseline not in (None, "") else None
    except (TypeError, ValueError):
        baseline = None

    return {
        "task_id": record["task_id"],
        "name": str(v.get("name") or record["task_id"]),
        "source_project": "custom",
        "category": "custom",
        "modality": record["modality"],
        "dataset_desc": dataset_desc,
        "eval_metric": str(v.get("eval_metric") or spec.get("default_metric", {}).get("eval_metric", "metric")),
        "direction": str(v.get("direction") or spec.get("default_metric", {}).get("direction", "higher")),
        "baseline": baseline,
        "reference": None,
        "gates": gates,
        "harness": record["harness"],
        "run_command": suggest_run_command(record["task_type"], v),
        "source_path": str(v.get("data_dir") or v.get("dataset_path") or v.get("prompt_dataset") or ""),
        "tags": list(v.get("tags") or []) + [record["task_type"]],
        "supported_by_platform": bool(record["executable"]),
        "note": f"自定义注册任务 · {record['label']} · 注册于 {record['created_at']}",
        "task_type": record["task_type"],
        "type_config": v,
    }
