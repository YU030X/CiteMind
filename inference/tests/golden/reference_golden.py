"""独立参考取证：离线生成 BAAI/bge-small-zh-v1.5 的 golden tokenizer IDs 与 512 维向量。

这是与项目 API 实现相互独立的参考计算，只用于后续集成 fixture，不构成项目代码/API 验收。
严格约束：
- 加载镜像内已烘入的固定产物 /models/bge-small-zh-v1.5（revision 7999e1d3...）。
- trust_remote_code=False、use_safetensors=True、CPU、float32、model.eval() + torch.inference_mode()。
- 上游模型卡 Transformers 路径：CLS pooling = last_hidden_state[:, 0]，再 L2 normalize。
- document 文本无任何检索前缀，原文不做 strip；add_special_tokens=True、truncation=False。
- 不联网，不读取任何凭据，不使用用户/企业资料。
"""

import hashlib
import json
import platform
import sys
from pathlib import Path

import torch
import tokenizers
import transformers
from transformers import AutoModel, AutoTokenizer

MODEL_ID = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_DIR = Path("/models/bge-small-zh-v1.5")
MANIFEST = Path("/models/model-manifest.json")
OUT = Path("/art/golden-reference.json")

# 与 inference/scripts/prepare_model.py 中钉死的期望摘要逐字节比对。
PINNED = {
    "config.json": (776, "3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a"),
    "tokenizer_config.json": (367, "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"),
    "tokenizer.json": (439125, "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    "special_tokens_map.json": (125, "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
    "vocab.txt": (109540, "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"),
    "model.safetensors": (95827648, "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"),
}

THREADS = 2

# 固定、自制、非敏感样本。sample 3 保留首尾空格，用于证明参考路径不做 strip。
SAMPLES = [
    {
        "id": "zh-plain",
        "note": "纯中文陈述句，无检索前缀",
        "text": "向量检索把语义相近的文档映射到同一空间。",
    },
    {
        "id": "en-mixed-case-digits-punct",
        "note": "英文大小写、数字、标点、空格与符号，用于锁定 tokenizer 语义",
        "text": "model v1.5: batch size 8, accuracy 0.98 (fp32). A/B #7.",
    },
    {
        "id": "zh-edge-whitespace-mixed",
        "note": "原文本保留首尾空格不 strip，中英混排含百分号与句号",
        "text": "  缓存 Cache 命中率 95% 。  ",
    },
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def embed(model, encoded) -> torch.Tensor:
    with torch.inference_mode():
        hidden = model(**encoded).last_hidden_state
    cls = hidden[:, 0]
    return torch.nn.functional.normalize(cls, p=2, dim=1)


def max_abs_diff(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a - b).abs().max().item())


def main() -> None:
    torch.set_num_threads(THREADS)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["model"] == MODEL_ID, manifest["model"]
    assert manifest["revision"] == REVISION, manifest["revision"]

    file_records = {}
    for name, (size, expected_sha) in PINNED.items():
        path = MODEL_DIR / name
        actual_size = path.stat().st_size
        actual_sha = sha256_file(path)
        file_records[name] = {
            "size": actual_size,
            "sha256": actual_sha,
            "matches_pinned": actual_size == size and actual_sha == expected_sha,
            "manifest_sha256": manifest["files"][name]["sha256"],
        }
        assert file_records[name]["matches_pinned"], name
        assert file_records[name]["manifest_sha256"] == expected_sha, name
    assert sorted(p.name for p in MODEL_DIR.iterdir()) == sorted(PINNED), "模型目录文件集合异常"

    tokenizer = AutoTokenizer.from_pretrained(
        str(MODEL_DIR), trust_remote_code=False, use_fast=True
    )
    model = AutoModel.from_pretrained(
        str(MODEL_DIR),
        trust_remote_code=False,
        use_safetensors=True,
        dtype=torch.float32,
    )
    model.eval()
    assert model.device.type == "cpu"
    assert model.config.hidden_size == 512, model.config.hidden_size

    texts = [s["text"] for s in SAMPLES]

    # 单条编码：truncation=False，含特殊 token，无 padding。
    singles = [
        tokenizer(text, add_special_tokens=True, truncation=False, padding=False, return_tensors="pt")
        for text in texts
    ]
    # 混合 batch：同一批同一顺序，右侧 padding。
    batch = tokenizer(
        texts, add_special_tokens=True, truncation=False, padding=True, return_tensors="pt"
    )

    single_vecs = [embed(model, enc) for enc in singles]
    batch_vec1 = embed(model, batch)
    batch_vec2 = embed(model, batch)  # 同批同序重复

    repeat_diff = max_abs_diff(batch_vec1, batch_vec2)
    mixed_diffs = {
        SAMPLES[i]["id"]: max_abs_diff(single_vecs[i], batch_vec1[i])
        for i in range(len(SAMPLES))
    }

    samples = []
    for i, spec in enumerate(SAMPLES):
        enc = singles[i]
        vec = single_vecs[i][0]
        token_ids = enc["input_ids"][0].tolist()
        tokens = tokenizer.convert_ids_to_tokens(token_ids)
        norm = float(vec.norm(p=2).item())
        assert vec.shape == (512,), vec.shape
        assert bool(torch.isfinite(vec).all()), spec["id"]
        samples.append(
            {
                "id": spec["id"],
                "note": spec["note"],
                "text": spec["text"],
                "text_length": len(spec["text"]),
                "add_special_tokens": True,
                "truncation": False,
                "padding": False,
                "token_ids": token_ids,
                "tokens": tokens,
                "attention_mask": enc["attention_mask"][0].tolist(),
                "token_count": len(token_ids),
                "dim": int(vec.shape[0]),
                "vector": [float(x) for x in vec.tolist()],
                "l2_norm": norm,
                "finite": True,
            }
        )

    result = {
        "artifact": "citemind-embedding-golden-reference",
        "purpose": "集成用独立参考 fixture；非项目 API/代码验收",
        "reference_method": {
            "model_id": MODEL_ID,
            "exact_revision": REVISION,
            "model_dir": str(MODEL_DIR),
            "trust_remote_code": False,
            "use_safetensors": True,
            "device": "cpu",
            "dtype": "float32",
            "eval_mode": "model.eval() + torch.inference_mode()",
            "tokenizer": "AutoTokenizer(use_fast=True), add_special_tokens=True, truncation=False",
            "pooling": "last_hidden_state[:, 0] (CLS) 然后 L2 normalize (p=2, dim=1)",
            "document_prefix": "无检索前缀",
            "text_normalization": "原文本不 strip，按给定字符串原样编码",
            "cross_env_note": (
                "同批同序与单条/混合 batch 的比较使用最大绝对差；"
                "跨 CPU/环境的浮点结果只保证在容差内一致，不声称字节级相同"
            ),
            "upstream_path": (
                "https://huggingface.co/BAAI/bge-small-zh-v1.5 模型卡 CLS pooling + normalize"
            ),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "tokenizers": tokenizers.__version__,
            "torch_num_threads": torch.get_num_threads(),
            "network": "none",
            "uid": 10002,
        },
        "model": {
            "id": MODEL_ID,
            "revision": REVISION,
            "hidden_size": int(model.config.hidden_size),
            "num_hidden_layers": int(model.config.num_hidden_layers),
            "max_position_embeddings": int(model.config.max_position_embeddings),
            "tokenizer_class": type(tokenizer).__name__,
            "is_fast": bool(tokenizer.is_fast),
            "do_lower_case_config": json.loads(
                (MODEL_DIR / "tokenizer_config.json").read_text(encoding="utf-8")
            ).get("do_lower_case"),
        },
        "files": file_records,
        "checks": {
            "repeat_same_batch_in_order_max_abs_diff": repeat_diff,
            "single_vs_mixed_batch_max_abs_diff": mixed_diffs,
            "single_vs_mixed_batch_tolerance": 1e-5,
            "all_vectors_finite": True,
            "all_dim_512": True,
        },
        "samples": samples,
    }

    OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print("WROTE", OUT)
    print("REPEAT_MAX_ABS_DIFF", repeat_diff)
    print("SINGLE_VS_MIXED_MAX_ABS_DIFF", json.dumps(mixed_diffs))
    print("TOKEN_COUNTS", [s["token_count"] for s in samples])
    print("NORMS", [s["l2_norm"] for s in samples])


if __name__ == "__main__":
    main()
