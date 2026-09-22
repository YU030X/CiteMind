"""构建期下载固定模型，并以 Hub commit 元数据 + 本文件钉死的摘要双重校验实际字节。

只有构建阶段联网：运行期镜像始终保持离线。校验同时使用上游 Git blob / LFS 摘要和本文件
记录的 SHA-256，因此「自报 revision 的文件」无法冒充产物身份。公共模型无需凭据，脚本不读取
HF_TOKEN、模型缓存或应用密钥。
"""

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
DESTINATION = Path("/models/bge-small-zh-v1.5")
# 清单放在模型目录之外：它只描述产物身份，不应参与模型目录自身的文件集合校验。
MANIFEST = "model-manifest.json"

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


def check_file(path: Path, metadata: dict[str, Any]) -> str:
    """按本文件钉死的摘要和上游元数据两套证据校验一个文件。"""

    expected = EXPECTED_FILES[path.name]
    sha256, blob = file_digests(path)
    if path.stat().st_size != metadata["size"]:
        raise ValueError(f"模型文件大小与上游元数据不符：{path.name}")
    if path.stat().st_size != expected["size"]:
        raise ValueError(f"模型文件大小不符：{path.name}")
    if sha256 != expected["sha256"]:
        raise ValueError(f"模型文件 SHA-256 与构建期钉死的摘要不符：{path.name}")
    lfs = metadata.get("lfs")
    if lfs:
        if sha256 != lfs["sha256"]:
            raise ValueError(f"上游 LFS SHA-256 不符：{path.name}")
    elif blob != metadata["blobId"]:
        raise ValueError(f"上游 Git blob 不符：{path.name}")
    return sha256


def fetch_upstream_metadata() -> dict[str, Any]:
    with urllib.request.urlopen(
        f"{HUB}/api/models/{MODEL}/revision/{REVISION}?blobs=true", timeout=60
    ) as response:
        return json.load(response)


def manifest_path(destination: Path) -> Path:
    return destination.parent / MANIFEST


def inspect_model_directory_files(destination: Path) -> None:
    if {path.name for path in destination.iterdir()} != set(FILES):
        raise ValueError("模型目录存在缺失或额外文件")


def prepare(destination: Path) -> None:
    manifest = manifest_path(destination)
    if manifest.exists():
        raise FileExistsError(f"拒绝覆盖已有产物清单：{manifest}")
    upstream = fetch_upstream_metadata()
    if upstream["sha"] != REVISION:
        raise ValueError("Hub 返回的 commit 与冻结 revision 不符")
    siblings = {item["rfilename"]: item for item in upstream["siblings"]}
    missing = [name for name in FILES if name not in siblings]
    if missing:
        raise ValueError(f"上游 revision 缺少产物文件：{', '.join(missing)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有模型目录：{destination}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "model"
        staging.mkdir(mode=0o755)
        records = {}
        for name in FILES:
            metadata = siblings[name]
            target = staging / name
            with urllib.request.urlopen(
                f"{HUB}/{MODEL}/resolve/{REVISION}/{name}", timeout=60
            ) as response, target.open("wb") as output:
                shutil.copyfileobj(response, output)
            records[name] = {
                "sha256": check_file(target, metadata),
                "size": metadata["size"],
                "blobId": metadata["blobId"],
                "lfs": metadata.get("lfs"),
            }
        manifest_data = {"model": MODEL, "revision": upstream["sha"], "files": records}
        (staging / MANIFEST).write_text(
            json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
        (destination / MANIFEST).rename(manifest_path(destination))
    verify(destination)


def verify(destination: Path) -> None:
    """离线复核已有产物：不联网，只比对清单、钉死摘要与文件集合。"""

    data = json.loads(manifest_path(destination).read_text(encoding="utf-8"))
    if data["model"] != MODEL or data["revision"] != REVISION:
        raise ValueError("模型产物清单与冻结身份不符")
    if set(data["files"]) != set(FILES):
        raise ValueError("模型产物清单文件集合不符")
    inspect_model_directory_files(destination)
    for name, metadata in data["files"].items():
        recorded = EXPECTED_FILES[name]
        if metadata["size"] != recorded["size"] or metadata["sha256"] != recorded["sha256"]:
            raise ValueError(f"产物清单摘要与钉死值不符：{name}")
        if check_file(destination / name, metadata) != recorded["sha256"]:
            raise ValueError(f"产物 SHA-256 不符：{name}")
    print(f"模型校验通过：{MODEL}@{REVISION}，{len(FILES)} 个文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    parser.add_argument("--verify", action="store_true", help="仅离线校验已有产物")
    args = parser.parse_args()
    if args.verify:
        verify(args.destination)
    else:
        prepare(args.destination)
