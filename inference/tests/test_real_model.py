"""真实 BGE 模型的显式 opt-in 验收：与独立 golden 参考逐项比较。

默认跳过：需要真实的本地权重目录与 ``CITEMIND_RUN_MODEL_TESTS=1``。显式 opt-in 后，模型
缺失、损坏或 golden 不一致都必须使测试失败，不能用跳过冒充通过。

golden 由独立断网 CPU 环境生成，只在此处做集成比较；token IDs 要求严格相等，512 维向量
使用最大绝对差 <= 1e-4 的容差比较，尚未实测跨 CPU。
"""

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support import TEST_TOKEN, build_settings

from citemind_inference.app import create_app
from citemind_inference.config import DEFAULT_EMBEDDING_MODEL_PATH, Settings
from citemind_inference.embeddings import Embedder, load_embedder

pytestmark = pytest.mark.model

OPT_IN_ENV = "CITEMIND_RUN_MODEL_TESTS"
MODEL_PATH_ENV = "CITEMIND_TEST_EMBEDDING_MODEL_PATH"
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "golden-reference.json"
# 由独立断网 CPU 环境生成并审核；cross_env_note 为文字说明，实测数值未改动。
GOLDEN_SHA256 = "967e700bc3baf8147fcfe8919c2b8a8e665a82f3bb2fc7ff7fcf33850b8eb057"
VECTOR_MAX_ABS_DIFF = 1e-4


def _require_opt_in() -> Path:
    if os.environ.get(OPT_IN_ENV) != "1":
        pytest.skip(f"设置 {OPT_IN_ENV}=1 才运行真实模型验收")
    path = Path(os.environ.get(MODEL_PATH_ENV, str(DEFAULT_EMBEDDING_MODEL_PATH)))
    if not path.is_dir():
        # 显式 opt-in 后，缺失模型必须是失败而不是跳过。
        pytest.fail(f"显式 opt-in 但本地模型目录不存在：{path}")
    return path


def _golden() -> dict[str, Any]:
    raw = GOLDEN_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != GOLDEN_SHA256:
        pytest.fail("golden 参考文件 SHA-256 与审核记录不符，可能已被篡改")
    parsed = json.loads(raw.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def real_settings() -> Settings:
    return build_settings(embedding_model_path=_require_opt_in())


@pytest.fixture(scope="module")
def real_embedder(real_settings: Settings) -> Embedder:
    return load_embedder(real_settings)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return _golden()


def max_abs_diff(left: list[float], right: list[float]) -> float:
    return max(abs(a - b) for a, b in zip(left, right, strict=True))


def test_real_tokenizer_ids_match_golden(
    real_settings: Settings, real_embedder: Embedder, golden: dict[str, Any]
) -> None:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(real_settings.embedding_model_path),
        local_files_only=True,
        trust_remote_code=False,
        use_fast=True,
    )
    for sample in golden["samples"]:
        encoded = tokenizer(sample["text"], add_special_tokens=True, truncation=False)
        assert list(encoded["input_ids"]) == sample["token_ids"], sample["id"]
        # 项目自带编码器报出的 token 数必须与 golden 一致，避免只比 tokenizer 而漏掉编码器。
        count = real_embedder.token_counts([sample["text"]])[0]
        assert count == sample["token_count"], sample["id"]


def test_real_single_vectors_match_golden(
    real_embedder: Embedder, golden: dict[str, Any]
) -> None:
    for sample in golden["samples"]:
        vector = real_embedder.embed([sample["text"]])[0]
        assert len(vector) == 512
        assert max_abs_diff(vector, sample["vector"]) <= VECTOR_MAX_ABS_DIFF, sample["id"]


def test_real_batch_vectors_match_golden(
    real_embedder: Embedder, golden: dict[str, Any]
) -> None:
    texts = [sample["text"] for sample in golden["samples"]]
    vectors = real_embedder.embed(texts)

    assert len(vectors) == len(golden["samples"])
    for vector, sample in zip(vectors, golden["samples"], strict=True):
        assert max_abs_diff(vector, sample["vector"]) <= VECTOR_MAX_ABS_DIFF, sample["id"]


def test_real_embedder_returns_normalized_512_dim_vectors(real_embedder: Embedder) -> None:
    texts = ["企业知识库的检索增强生成", "another plain sentence"]

    vectors = real_embedder.embed(texts)

    assert real_embedder.dimension == 512
    assert len(vectors) == len(texts)
    for vector in vectors:
        assert len(vector) == 512
        norm = math.sqrt(sum(value * value for value in vector))
        assert norm == pytest.approx(1.0, abs=1e-4)


def test_real_embedder_is_deterministic(real_embedder: Embedder) -> None:
    first = real_embedder.embed(["同一条文本"])
    second = real_embedder.embed(["同一条文本"])

    assert first == second


def test_real_token_counts_include_special_tokens(real_embedder: Embedder) -> None:
    counts = real_embedder.token_counts(["你好，世界"])

    # [CLS] 与 [SEP] 至少各占一个 token。
    assert len(counts) == 1
    assert counts[0] >= 2
    assert counts[0] <= real_embedder.max_tokens


def test_real_app_embeds_and_rejects_over_512_tokens(real_settings: Settings) -> None:
    app = create_app(real_settings, embedder_factory=load_embedder)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

    with TestClient(app) as client:
        accepted = client.post(
            "/internal/embed",
            headers=headers,
            json={"kind": "document", "texts": ["真实模型验收文本"]},
        )
        oversized = client.post(
            "/internal/embed",
            headers=headers,
            json={"kind": "document", "texts": ["知" * 600]},
        )

    assert accepted.status_code == 200
    assert accepted.json()["dimension"] == 512
    assert accepted.json()["modelRevision"] == real_settings.embedding_model_revision
    assert accepted.json()["tokenCounts"][0] > 0

    # 超过模型 512 位置上限且不截断，必须明确拒绝。
    assert oversized.status_code == 422
    assert oversized.json()["code"] == "EMBEDDING_INPUT_TOO_LONG"
