"""Phase 1 开发评估集的最小离线工具与可选真实 producer。

本包提供三件事，不建评估平台、不建新数据库、不用 LLM 裁判：

- ``dataset``：开发题集与自制语料清单的严格 schema、语料存在性与 gold 引用匹配校验、
  无权限/删除场景的题集自泄漏校验。
- ``metrics``：基于**已有真实结果文件**的确定性指标计算；没有结果文件就不产生任何数字。
- ``runner``：默认 dry-run 的最小结果 producer；只有显式 opt-in 并给出请求硬上限时才走真实 API
  与只读数据库，把 40 题结果写到结果文件供 ``metrics`` 消费。

开发集（``datasetKind="dev"``）不是留出集或测试集，其结构可离线验证，但真实质量指标必须
在留出集上按固定分母、真实模型与真实权限环境测量，不能把开发集结果当最终结论。

Phase 3 第 2 片新增纯离线模块：``ranking_metrics``（固定 Recall@10/nDCG@10）、``calibration``
（拒答阈值扫描）、``ablation``（A/B/C 产物 schema 与三元组校验）与 ``analysis``
（``python -m rag_backend.evaluation.analysis`` 只读题集与三个产物的离线入口）。它们不联网、
不调用模型，也不产生真实指标数值。
"""

from rag_backend.evaluation.dataset import (
    CorpusManifest,
    DatasetValidationError,
    EvaluationDataset,
    EvaluationQuestion,
    GoldSpan,
    ValidationReport,
    load_dataset,
    load_manifest,
    validate_dataset,
)
from rag_backend.evaluation.metrics import (
    EvaluationResults,
    MetricsReport,
    compute_metrics,
)

__all__ = [
    "CorpusManifest",
    "DatasetValidationError",
    "EvaluationDataset",
    "EvaluationQuestion",
    "EvaluationResults",
    "GoldSpan",
    "MetricsReport",
    "ValidationReport",
    "compute_metrics",
    "load_dataset",
    "load_manifest",
    "validate_dataset",
]
