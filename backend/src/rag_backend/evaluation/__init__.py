"""Phase 1 开发评估集的最小离线工具。

本包只提供两件事，不建评估平台、不建数据库、不调用任何 LLM：

- ``dataset``：开发题集与自制语料清单的严格 schema、语料存在性与 gold 引用匹配校验、
  无权限/删除场景的题集自泄漏校验。
- ``metrics``：基于**已有真实结果文件**的确定性指标计算；没有结果文件就不产生任何数字。

开发集（``datasetKind="dev"``）不是留出集或测试集，其结构可离线验证，但真实质量指标必须
在留出集上按固定分母、真实模型与真实权限环境测量，不能把开发集结果当最终结论。
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
