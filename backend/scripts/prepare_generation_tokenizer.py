"""构建期取得固定 revision 的 DeepSeek V4.1 tokenizer 产物，并用两套证据校验实际字节。

只有 API 镜像的构建阶段联网：运行期镜像不下载、不读任何凭据、不挂载宿主模型卷。产物身份同时由
Hub 返回的 revision/commit 与 ``rag_backend.generation.deepseek_token_counting`` 里钉死的字节大小、
SHA-256 约束，因此「自报 revision 的文件」无法冒充产物身份。公共模型无需凭据，脚本不读取
``HF_TOKEN``、模型缓存或应用密钥，也不打印正文或 URL 之外的任何内容。

用法（均从仓库根目录执行，单行）：
  python backend/scripts/prepare_generation_tokenizer.py
  python backend/scripts/prepare_generation_tokenizer.py --verify
"""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

from rag_backend.generation.deepseek_token_counting import (
    DEFAULT_TOKENIZER_DIRECTORY,
    TOKENIZER_ARTIFACTS,
    TOKENIZER_MODEL_REPOSITORY,
    TOKENIZER_MODEL_REVISION,
    verify_tokenizer_directory,
)

HUB = "https://huggingface.co"
ARTIFACT_NAME = "tokenizer.json"


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


def fetch_upstream_metadata() -> dict:
    """读取固定 revision 的 Hub 元数据；只用于交叉校验，不作为身份自证。"""

    url = f"{HUB}/api/models/{TOKENIZER_MODEL_REPOSITORY}/revision/{TOKENIZER_MODEL_REVISION}"
    with urllib.request.urlopen(f"{url}?blobs=true", timeout=60) as response:
        return json.load(response)


def prepare(destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有 tokenizer 产物目录：{destination}")

    upstream = fetch_upstream_metadata()
    if upstream["sha"] != TOKENIZER_MODEL_REVISION:
        raise ValueError("Hub 返回的 commit 与钉死 revision 不符")
    siblings = {str(item["rfilename"]): item for item in upstream["siblings"]}
    if ARTIFACT_NAME not in siblings:
        raise ValueError(f"上游 revision 缺少产物文件：{ARTIFACT_NAME}")
    metadata = siblings[ARTIFACT_NAME]
    expected_size, expected_sha256 = TOKENIZER_ARTIFACTS[ARTIFACT_NAME]

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / destination.name
        staging.mkdir(mode=0o755)
        target = staging / ARTIFACT_NAME
        source = (
            f"{HUB}/{TOKENIZER_MODEL_REPOSITORY}/resolve/"
            f"{TOKENIZER_MODEL_REVISION}/{ARTIFACT_NAME}"
        )
        with urllib.request.urlopen(source, timeout=120) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)

        if target.stat().st_size != expected_size:
            raise ValueError(f"产物大小与钉死值不符：{ARTIFACT_NAME}")
        if target.stat().st_size != metadata["size"]:
            raise ValueError(f"产物大小与上游元数据不符：{ARTIFACT_NAME}")
        actual_sha256, actual_blob = file_digests(target)
        if actual_sha256 != expected_sha256:
            raise ValueError(f"产物 SHA-256 与钉死值不符：{ARTIFACT_NAME}")
        # HF 非 LFS 文件只提供 Git blob SHA-1；LFS 文件另给内容 SHA-256。
        lfs = metadata.get("lfs")
        if lfs:
            if actual_sha256 != lfs["sha256"]:
                raise ValueError(f"上游 LFS SHA-256 不符：{ARTIFACT_NAME}")
        elif actual_blob != metadata["blobId"]:
            raise ValueError(f"上游 Git blob 不符：{ARTIFACT_NAME}")

        # 复用运行期同一套目录集合校验，构建期与启动期判定必须一致。
        verify_tokenizer_directory(staging)
        staging.rename(destination)

    verify(destination)


def verify(destination: Path) -> None:
    """离线复核已有产物：不联网，只按钉死摘要与目录集合核对实际字节。"""

    verify_tokenizer_directory(destination)
    print(
        f"tokenizer 产物校验通过：{TOKENIZER_MODEL_REPOSITORY}@{TOKENIZER_MODEL_REVISION}，"
        f"{len(TOKENIZER_ARTIFACTS)} 个文件"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_TOKENIZER_DIRECTORY)
    parser.add_argument("--verify", action="store_true", help="仅离线校验已有产物")
    arguments = parser.parse_args()
    if arguments.verify:
        verify(arguments.destination)
    else:
        prepare(arguments.destination)
    sys.exit(0)
