"""构建期下载固定模型，并以 Hub commit 元数据 + 钉死摘要双重校验实际字节。

只有构建阶段联网：运行期镜像始终保持离线。校验同时使用上游 Git blob / LFS 摘要和本文件
记录的 SHA-256，因此「自报 revision 的文件」无法冒充产物身份。公共模型无需凭据，脚本不读取
HF_TOKEN、模型缓存或应用密钥。

``--model embedding``（默认）沿用原先的 BAAI/bge-small-zh-v1.5 六件钉死摘要。
``--model rerank`` 对应 BAAI/bge-reranker-base：本仓库**没有**预先下载核验过的 SHA-256，因此
不写死摘要（不伪造身份），改为在构建期用 Hub 元数据逐个核验后，把实际字节大小与 SHA-256
写进 ``rerank-model-manifest.json``，运行期再按该清单离线核对。首次真实构建后应人工确认清单
来源并把结论写进 Agent Note。
"""

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
DESTINATION = Path("/models/bge-small-zh-v1.5")
# 清单放在模型目录之外：它只描述产物身份，不应参与模型目录自身的文件集合校验。
MANIFEST = "model-manifest.json"

RERANK_MODEL = "BAAI/bge-reranker-base"
RERANK_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
RERANK_DESTINATION = Path("/models/bge-reranker-base")
# reranker 与 embedding 同处一个父目录，必须使用不同的清单文件名。
RERANK_MANIFEST = "rerank-model-manifest.json"
# reranker 的上游文件集：这些文件都在固定 revision 的 Hub 元数据里核验；缺一个即构建失败。
# 本文件不写死任何 SHA-256，摘要来自真实下载后的字节并由清单锁定。
RERANK_FILES = (
    "config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "model.safetensors",
)

# 只烘入加载所需的配置、tokenizer 与 safetensors；pytorch_model.bin 是同一权重的冗余副本。
# size 与 sha256 是固定 revision 下实际下载核对过的事实（Hub 非 LFS 文件另有 blobId 校验）。
EXPECTED_FILES: dict[str, dict[str, Any]] = {
    "config.json": {
        "size": 776,
        "sha256": "3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a",
    },
    "tokenizer_config.json": {
        "size": 367,
        "sha256": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    },
    "tokenizer.json": {
        "size": 439125,
        "sha256": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    },
    "special_tokens_map.json": {
        "size": 125,
        "sha256": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    },
    "vocab.txt": {
        "size": 109540,
        "sha256": "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
    },
    "model.safetensors": {
        "size": 95827648,
        "sha256": "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026",
    },
}
FILES = tuple(EXPECTED_FILES)
HUB = "https://huggingface.co"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """一次构建期下载的目标身份；``pinned_digests`` 为 None 表示摘要由构建期生成。"""

    model: str
    revision: str
    destination: Path
    manifest_name: str
    files: tuple[str, ...]
    pinned_digests: dict[str, dict[str, Any]] | None


EMBEDDING_SPEC = ModelSpec(MODEL, REVISION, DESTINATION, MANIFEST, FILES, EXPECTED_FILES)
RERANK_SPEC = ModelSpec(
    RERANK_MODEL, RERANK_REVISION, RERANK_DESTINATION, RERANK_MANIFEST, RERANK_FILES, None
)
SPECS = {"embedding": EMBEDDING_SPEC, "rerank": RERANK_SPEC}


def file_digests(path: Path) -> tuple[str, str]:
    """同时计算内容 SHA-256 和 Git blob SHA-1（后者含 Git 对象头）。"""

    sha256 = hashlib.sha256()
    blob = hashlib.sha1(usedforsecurity=False)
    blob.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha256.update(chunk)
            blob.update(chunk)
    return sha256.hexdigest(), blob.hexdigest()


def check_file(path: Path, metadata: dict[str, Any], spec: ModelSpec) -> str:
    """按本文件钉死的摘要（若有）和上游元数据两套证据校验一个文件。"""

    sha256, blob = file_digests(path)
    if path.stat().st_size != metadata["size"]:
        raise ValueError(f"模型文件大小与上游元数据不符：{path.name}")
    if spec.pinned_digests is not None:
        expected = spec.pinned_digests[path.name]
        if path.stat().st_size != expected["size"]:
            raise ValueError(f"模型文件大小不符：{path.name}")
        if sha256 != expected["sha256"]:
            raise ValueError(f"模型文件 SHA-256 与构建期钉死的摘要不符：{path.name}")
    lfs = metadata.get("lfs")
    if lfs:
        if sha256 != lfs["sha256"]:
            raise ValueError(f"上游 LFS SHA-256 不符：{path.name}")
    elif metadata.get("blobId"):
        if blob != metadata["blobId"]:
            raise ValueError(f"上游 Git blob 不符：{path.name}")
    return sha256


def fetch_upstream_metadata(spec: ModelSpec) -> dict[str, Any]:
    with urllib.request.urlopen(
        f"{HUB}/api/models/{spec.model}/revision/{spec.revision}?blobs=true", timeout=60
    ) as response:
        return json.load(response)


def manifest_path(destination: Path, spec: ModelSpec) -> Path:
    return destination.parent / spec.manifest_name


def inspect_model_directory_files(destination: Path, spec: ModelSpec) -> None:
    if {path.name for path in destination.iterdir()} != set(spec.files):
        raise ValueError("模型目录存在缺失或额外文件")


def prepare(destination: Path, spec: ModelSpec) -> None:
    manifest = manifest_path(destination, spec)
    if manifest.exists():
        raise FileExistsError(f"拒绝覆盖已有产物清单：{manifest}")
    upstream = fetch_upstream_metadata(spec)
    if upstream["sha"] != spec.revision:
        raise ValueError("Hub 返回的 commit 与冻结 revision 不符")
    siblings = {item["rfilename"]: item for item in upstream["siblings"]}
    missing = [name for name in spec.files if name not in siblings]
    if missing:
        raise ValueError(f"上游 revision 缺少产物文件：{', '.join(missing)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有模型目录：{destination}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "model"
        staging.mkdir(mode=0o755)
        records = {}
        for name in spec.files:
            metadata = siblings[name]
            target = staging / name
            with urllib.request.urlopen(
                f"{HUB}/{spec.model}/resolve/{spec.revision}/{name}", timeout=60
            ) as response, target.open("wb") as output:
                shutil.copyfileobj(response, output)
            records[name] = {
                "sha256": check_file(target, metadata, spec),
                "size": metadata["size"],
                "blobId": metadata["blobId"],
                "lfs": metadata.get("lfs"),
            }
        manifest_data = {"model": spec.model, "revision": upstream["sha"], "files": records}
        (staging / spec.manifest_name).write_text(
            json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
        (destination / spec.manifest_name).rename(manifest_path(destination, spec))
    verify(destination, spec)


def verify(destination: Path, spec: ModelSpec) -> None:
    """离线复核已有产物：不联网，只比对清单、钉死摘要（若有）与文件集合。"""

    data = json.loads(manifest_path(destination, spec).read_text(encoding="utf-8"))
    if data["model"] != spec.model or data["revision"] != spec.revision:
        raise ValueError("模型产物清单与冻结身份不符")
    if set(data["files"]) != set(spec.files):
        raise ValueError("模型产物清单文件集合不符")
    inspect_model_directory_files(destination, spec)
    for name, metadata in data["files"].items():
        if spec.pinned_digests is not None:
            recorded = spec.pinned_digests[name]
            if metadata["size"] != recorded["size"] or metadata["sha256"] != recorded["sha256"]:
                raise ValueError(f"产物清单摘要与钉死值不符：{name}")
        if check_file(destination / name, metadata, spec) != metadata["sha256"]:
            raise ValueError(f"产物 SHA-256 不符：{name}")
    print(f"模型校验通过：{spec.model}@{spec.revision}，{len(spec.files)} 个文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", choices=sorted(SPECS), default="embedding", help="要准备/校验的模型"
    )
    parser.add_argument("--destination", type=Path, default=None)
    parser.add_argument("--verify", action="store_true", help="仅离线校验已有产物")
    args = parser.parse_args()
    spec = SPECS[args.model]
    destination = args.destination if args.destination is not None else spec.destination
    if args.verify:
        verify(destination, spec)
    else:
        prepare(destination, spec)
