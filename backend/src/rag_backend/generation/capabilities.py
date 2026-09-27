"""服务端生成能力白名单：只暴露经证实与固定 tokenizer/渲染契约兼容的模型与思考选项。

DeepSeek 官方文档（``https://api-docs.deepseek.com``，2026-09-27 读取）列出的 Chat Completions
模型是 ``deepseek-flash`` 与 ``deepseek-v4-pro``，``thinking`` 取 ``enabled``/``disabled``。
本仓库只钉死了 ``deepseek-flash``（DeepSeek-V4.1-Flash 家族）的 tokenizer 产物与 recipe V4.1
渲染契约，``deepseek-v4-pro`` 的词表与提示模板未经本仓库验证，因此**不进入白名单、前端也不
展示**：宁可只暴露一个真正支持单项的模型，也不给未验证模型一个假可用的入口。

本仓库只暴露经本项目验证的思考强度 ``low``/``high``/``max``；关闭思考由
``thinking.type=disabled`` 表达，不作为强度取值。recipe 适配器或公开文档里出现的其它
``reasoning_effort`` 取值不能据此推断为云 API 支持，故不纳入白名单。本模块只做静态白名单
判断，不联网、不读配置、不调用 provider；真实可用性仍以一次真实响应为准。
"""

from __future__ import annotations

from dataclasses import dataclass

from rag_backend.generation.deepseek_prompt import (
    DEFAULT_REASONING_EFFORT,
    SUPPORTED_REASONING_EFFORTS,
    ReasoningEffort,
)

# 已验证与固定 tokenizer/渲染契约兼容的模型；顺序即展示顺序，第一项是服务端默认。
SUPPORTED_MODEL_IDS: tuple[str, ...] = ("deepseek-flash",)
DEFAULT_MODEL_ID: str = SUPPORTED_MODEL_IDS[0]


def is_supported_model(model: str) -> bool:
    """模型是否属于服务端已验证白名单；不做大小写或别名归一，未知一律不接受。"""

    return model in SUPPORTED_MODEL_IDS


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCapability:
    """单个受支持模型的可切换能力；当前只有 thinking 开关与强度。"""

    model_id: str
    thinking_supported: bool = True
    efforts: tuple[ReasoningEffort, ...] = SUPPORTED_REASONING_EFFORTS
    default_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT


SUPPORTED_MODELS: tuple[ModelCapability, ...] = tuple(
    ModelCapability(model_id=model_id) for model_id in SUPPORTED_MODEL_IDS
)


__all__ = [
    "DEFAULT_MODEL_ID",
    "DEFAULT_REASONING_EFFORT",
    "SUPPORTED_MODELS",
    "SUPPORTED_MODEL_IDS",
    "SUPPORTED_REASONING_EFFORTS",
    "ModelCapability",
    "ReasoningEffort",
    "is_supported_model",
]
