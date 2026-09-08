"""Registry of infrastructure-layer capabilities (agent-callable tools).

Mirrors the ten R&D infrastructure layers (spec §2). Layers with a real execution-plane
adapter (③ eval, ⑧ experience, ⑩ adversarial) are bound to it; the rest get a
``StubCapabilityExecutor`` so the agent always has a working, audited tool to call.
Swap a stub for the real adapter by registering that layer's ``StageExecutor``.
"""

from __future__ import annotations

from .base import InfraCapability
from .executors import StubCapabilityExecutor
from .kaggle_eval_executor import KaggleEvalExecutor
from .badcase_retrain_executor import BadcaseRetrainExecutor
from .audit_executor import AuditExecutor
from .self_evolution_executor import SelfEvolutionExecutor
from .sandbox_executor import SandboxResearchExecutor
from ..executors import AttackExecutor
from ..executors import EvalExecutor
from ..executors import LessonExecutor

# (capability_id, layer_code, layer_name, title, description, artifact_type, real_executor?)
_CAPABILITIES: list[tuple[str, str, str, str, str, str, object | None]] = [
    (
        "layer_01_literature_research",
        "01_literature_research",
        "文献检索",
        "检索安全研究文献",
        "检索并汇总与对抗安全/对齐相关的论文、基准与数据集",
        "paper_set",
        None,
    ),
    (
        "layer_02_idea_generation_evaluation",
        "02_idea_generation_evaluation",
        "idea生成与评估校验",
        "生成并校验研究 idea",
        "基于文献与 baselines 生成研究假设并做可行性/新颖性校验",
        "idea_card",
        None,
    ),
    (
        "layer_03_eval",
        "03_eval",
        "评估与基准",
        "运行评测基准",
        "在指定 benchmark 上评测目标模型并产出 gate 结果",
        "eval_report",
        EvalExecutor(),
    ),
    (
        "layer_04_experiment_design",
        "04_experiment_design",
        "实验设计",
        "设计实验方案",
        "产出可复现的训练/评测实验设计规格",
        "design_spec",
        None,
    ),
    (
        "layer_05_data_evaluation_cleaning",
        "05_data_evaluation_cleaning",
        "数据评估清洗",
        "评估并清洗数据集",
        "评估数据质量、去毒/去重并产出清洗后数据集",
        "dataset_release",
        None,
    ),
    (
        "layer_06_code_development",
        "06_code_development",
        "代码开发",
        "编写实验代码",
        "实现训练/评测/红队脚本并产出代码补丁",
        "code_patch",
        None,
    ),
    (
        "layer_07_experiment_execution",
        "07_experiment_execution",
        "实验执行",
        "执行实验",
        "运行训练/评测实验并产出模型检查点",
        "model_checkpoint",
        None,
    ),
    (
        "layer_08_result_analysis_experience",
        "08_result_analysis_experience",
        "结果分析与经验生成",
        "分析结果并提炼经验",
        "从实验结果提炼可复用经验卡（回注对象）",
        "lesson_card",
        LessonExecutor(),
    ),
    (
        "layer_09_self_iterative_evolution",
        "09_self_iterative_evolution",
        "自迭代进化（递归改进元循环）",
        "改进研究过程而非工件",
        "递归改进元循环：抽取机制载体（搜索顺序/路由偏好），与策略归档比较，"
        "validate-and-revert 注入改进提案，发出 ImprovementAppliedEvent（可回滚）",
        "improvement_report",
        SelfEvolutionExecutor(),
    ),
    (
        "layer_10_adversarial_data_generation",
        "10_adversarial_data_generation",
        "数据生成对抗",
        "执行对抗红队",
        "生成对抗样本并评测鲁棒性（ASR/保留率）",
        "attack_report",
        AttackExecutor(),
    ),
    (
        "kaggle_eval",
        "kaggle_eval",
        "Kaggle 真实评测",
        "运行真实 Kaggle 评测",
        "加载公开 Kaggle 竞赛数据，训练真实 sklearn 模型并以交叉验证计算 accuracy/f1，"
        "发出真实 EvalCompletedEvent（用于端到端 agent 驱动的实证研究）",
        "eval_report",
        KaggleEvalExecutor(),
    ),
    (
        "badcase_retrain",
        "badcase_retrain",
        "Badcase 飞轮重训",
        "飞轮闭环迭代：坏例回放重训 + 回归门",
        "B 飞轮型最小闭环：读取已标注 badcase CSV，冻结架构下用自适应 badcase:original 配比"
        "回放重训，并在冻结的原始评测集上做回归门（不退化护栏）。badcase 召回提升为收益、"
        "回归不退化是硬约束，二者同时满足才接受新模型（发出真实 EvalCompletedEvent）。",
        "eval_report",
        BadcaseRetrainExecutor(),
    ),
    (
        "layer_11_external_audit",
        "layer_11_external_audit",
        "外部审计（双循环外环）",
        "对内部研究答案做约束级独立审计",
        "独立验证器/评审对内部研究答案做约束级审计，发出 AuditCompletedEvent（confidence s / "
        "recoverable v），驱动 Accept/Refine/Restart；破除自确认",
        "audit_report",
        AuditExecutor(),
    ),
    (
        "kaggle_eval_sandbox",
        "kaggle_eval_sandbox",
        "Kaggle 真实评测（Docker 沙箱）",
        "在隔离容器内运行真实 Kaggle 评测",
        "agent 模式的 kaggle_eval：研究命令在一次性 Docker 容器内执行（只读数据、无网络、掉权），"
        "产出真实 EvalCompletedEvent。由 run_capability 在 AGENT_SANDBOX=1 时自动路由。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
    (
        "run_research_sandbox",
        "run_research_sandbox",
        "容器内通用研究执行",
        "在隔离容器内运行任意研究命令",
        "agent 把任意研究命令（research_cmd，容器内路径 /repo/...、/data/...）放进一次性 Docker 沙箱执行，"
        "读回 result.json 并发出真实 EvalCompletedEvent。供 16 个非原生 tracked-only 任务接入容器内真跑。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
    (
        "text_cls_sandbox",
        "text_cls_sandbox",
        "文本分类评测（Docker 沙箱）",
        "在隔离容器内运行真实文本分类评测",
        "agent 模式的 text_classification：TF-IDF + 线性模型在一次性 Docker 容器内训练/评测（只读数据、"
        "无网络），产出真实 EvalCompletedEvent。由 run_capability 在 AGENT_SANDBOX=1 时自动路由。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
    (
        "image_cls_sandbox",
        "image_cls_sandbox",
        "图像分类评测（Docker 沙箱）",
        "在隔离容器内运行真实图像分类评测",
        "agent 模式的 image_classification：torchvision / 轻量 CNN backbone 在一次性 Docker 容器内训练/评测"
        "（只读数据、无网络），产出真实 EvalCompletedEvent。由 run_capability 在 AGENT_SANDBOX=1 时自动路由。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
    (
        "audio_cls_sandbox",
        "audio_cls_sandbox",
        "音频分类评测（Docker 沙箱）",
        "在隔离容器内运行真实音频分类评测",
        "agent 模式的 audio_classification：logmel/MFCC 特征 + 轻量 CNN 在一次性 Docker 容器内训练/评测"
        "（只读数据、无网络），产出真实 EvalCompletedEvent。由 run_capability 在 AGENT_SANDBOX=1 时自动路由。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
    (
        "embedding_sandbox",
        "embedding_sandbox",
        "Embedding 对比学习评测（Docker 沙箱）",
        "在隔离容器内运行真实 Embedding 评测",
        "agent 模式的 embedding_contrastive：文-文对比学习（TF-IDF + 线性映射 InfoNCE）在一次性 Docker 容器内"
        "训练/评测（只读数据、无网络），以检索 Recall@K 为指标产出真实 EvalCompletedEvent。",
        "eval_report",
        SandboxResearchExecutor(),
    ),
]

_PARAM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "params": {
            "type": "object",
            "description": "layer-specific parameters forwarded to the executor",
        },
    },
    "additionalProperties": True,
}


class CapabilityRegistry:
    """Holds the registered infrastructure-layer capabilities and resolves by id."""

    def __init__(self) -> None:
        self._caps: dict[str, InfraCapability] = {}
        self._by_layer: dict[str, InfraCapability] = {}

    def register(self, cap: InfraCapability) -> None:
        self._caps[cap.capability_id] = cap
        self._by_layer[cap.layer_code] = cap

    def resolve(self, capability_id: str) -> InfraCapability | None:
        return self._caps.get(capability_id)

    def resolve_by_layer(self, layer_code: str) -> InfraCapability | None:
        return self._by_layer.get(layer_code)

    def list_capabilities(self) -> list[dict[str, object]]:
        """The ten infrastructure-layer capabilities (agent-callable tools)."""
        return [cap.to_catalog_entry() for cap in self._caps.values() if cap.is_infra]

    def list_all_capabilities(self) -> list[dict[str, object]]:
        """Every registered capability, including extra non-infra ones (e.g. kaggle_eval).

        Use this for the agent-facing protocol so the agent can discover *all* callable
        tools, not just the ten infra layers.
        """
        return [c.to_catalog_entry() for c in self._caps.values()]


def default_capability_registry() -> CapabilityRegistry:
    """Registry wired with all ten layers (real adapters where they exist)."""

    reg = CapabilityRegistry()
    # Capabilities that are *extra* (demo / dual-loop) rather than one of the ten
    # R&D infrastructure layers — excluded from the infra-layer catalog but still
    # discoverable by an agent via list_all_capabilities().
    _NON_INFRA = {"kaggle_eval", "badcase_retrain", "layer_11_external_audit", "kaggle_eval_sandbox", "run_research_sandbox", "text_cls_sandbox", "image_cls_sandbox", "audio_cls_sandbox", "embedding_sandbox"}
    for cid, lcode, lname, title, desc, atype, ex in _CAPABILITIES:
        is_infra = cid not in _NON_INFRA
        reg.register(
            InfraCapability(
                capability_id=cid,
                layer_code=lcode,
                layer_name=lname,
                title=title,
                description=desc,
                param_schema=_PARAM_SCHEMA,
                executor=ex if ex is not None else StubCapabilityExecutor(lcode, atype),
                artifact_type=atype,
                is_infra=is_infra,
            )
        )
    return reg
