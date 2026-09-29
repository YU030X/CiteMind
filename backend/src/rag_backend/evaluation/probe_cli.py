"""``python -m rag_backend.evaluation.probe_cli``：Phase 3 只读 A/B/C 消融探针入口。

默认 dry-run：只读取题集、环境描述与资产映射，校验 scope KB/角色覆盖与预算最坏下界；**不读取
数据库 DSN/token 环境变量、不构造任何客户端、不写文件**，打印静态摘要退出 0。真实运行必须显式
``--allow-real-probe`` 与 ``--allow-real-rerank``；``datasetKind=holdout`` 另需
``--confirm-holdout``。

真实运行从指定环境变量（默认 ``EVAL_DATABASE_URL``/``INFERENCE_TOKEN``，不读 ``.env``）读取值，
缺失即静态失败。产物先驻留内存并通过三组 artifact 与 calibration 校验，再在同一输出目录写唯一
临时文件，四份全部写成功后才 ``os.replace`` 为正式文件；任何校验或运行失败都不会创建正式文件。
错误消息静态，不回显 query/text/token/DSN/username/UUID，也不打印 traceback。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from rag_backend.config import (
    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    DEFAULT_RERANK_TIMEOUT_SECONDS,
)
from rag_backend.evaluation.ablation import (
    AblationValidationError,
    validate_ablation_triplet,
)
from rag_backend.evaluation.calibration import CalibrationArtifact
from rag_backend.evaluation.dataset import (
    DatasetValidationError,
    EvaluationDataset,
    load_dataset,
)
from rag_backend.evaluation.probe import (
    BudgetCounter,
    ProbeError,
    ProbeInputs,
    ProbeOutcome,
    run_ablation_probe,
)
from rag_backend.evaluation.probe_adapters import (
    ProbeAdapterError,
    ProbeProfileIdentity,
    ProbeRuntime,
    build_probe_runtime,
    build_role_identities,
    resolve_single_organization,
    resolve_single_profile,
    validate_probe_database_url,
)
from rag_backend.evaluation.runner import (
    AssetRegistry,
    DatasetRunError,
    EnvironmentDescriptor,
    load_asset_map,
)
from rag_backend.retrieval.query_embedding_client import QUERY_ENCODING_CONTRACT

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATASET = _REPO_ROOT / "tests" / "evaluation" / "dev-questions.json"

ARTIFACT_FILENAMES = (
    "a-vector.json",
    "b-rrf.json",
    "c-rerank.json",
    "calibration.json",
)

# 探针固定口径：标定来源与分数口径为 B 的 RRF fusionScore；延迟为逐题/逐变体墙钟毫秒。
PROBE_CONFIG: dict[str, str | int | float | bool] = {
    "calibrationSource": "B_RRF",
    "calibrationScoreField": "fusionScore",
    "latencyUnit": "ms",
    "latencyScope": "perQuestionPerVariant",
}


class ProbeCliError(Exception):
    """CLI 静态错误；消息不回显 query/text/token/DSN/username/UUID。"""


@dataclass(frozen=True)
class _Prepared:
    dataset: EvaluationDataset
    descriptor: EnvironmentDescriptor
    registry: AssetRegistry


@dataclass(frozen=True)
class ProbeExecution:
    """一次真实运行的显式执行参数。"""

    max_embedding_requests: int
    max_rerank_requests: int
    created_at: str


RuntimeBuilder = Callable[..., ProbeRuntime]
ProbeExecutor = Callable[
    [ProbeRuntime, _Prepared, ProbeExecution], Awaitable[ProbeOutcome]
]
Clock = Callable[[], str]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.probe_cli",
        description="Phase 3 只读 A/B/C 消融探针（默认 dry-run，不连数据库、不调用模型）。",
    )
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--descriptor", type=Path, required=True, help="隔离环境描述 JSON")
    parser.add_argument("--asset-map", type=Path, required=True, help="显式资产映射 JSON")
    parser.add_argument("--out-dir", type=Path, required=True, help="产物输出目录（必须已存在）")
    parser.add_argument(
        "--database-url-env",
        default="EVAL_DATABASE_URL",
        help="只读数据库 DSN 的环境变量名（默认 EVAL_DATABASE_URL）",
    )
    parser.add_argument(
        "--inference-token-env",
        default="INFERENCE_TOKEN",
        help="内部 inference token 的环境变量名（默认 INFERENCE_TOKEN）",
    )
    parser.add_argument("--inference-base-url", default=None, help="真实运行必填；inference 基址")
    parser.add_argument(
        "--embedding-timeout-seconds",
        type=float,
        default=DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--rerank-timeout-seconds",
        type=float,
        default=DEFAULT_RERANK_TIMEOUT_SECONDS,
    )
    parser.add_argument("--max-embedding-requests", type=int, default=0)
    parser.add_argument("--max-rerank-requests", type=int, default=0)
    parser.add_argument(
        "--allow-real-probe",
        action="store_true",
        help="显式开启真实只读探针；默认 dry-run",
    )
    parser.add_argument(
        "--allow-real-rerank",
        action="store_true",
        help="显式允许真实 rerank 调用",
    )
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="真实运行 holdout 题集时必须显式确认；dry-run 不要求",
    )
    parser.add_argument(
        "--allow-database-name",
        default=None,
        help="数据库名不以 _test 结尾时，显式重申其数据库名",
    )
    return parser.parse_args(argv)


def _validate_numeric_args(args: argparse.Namespace) -> None:
    for label, value in (
        ("embedding 超时", args.embedding_timeout_seconds),
        ("rerank 超时", args.rerank_timeout_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ProbeCliError(f"{label}必须是有限正数")
    for label, value in (
        ("embedding 预算", args.max_embedding_requests),
        ("rerank 预算", args.max_rerank_requests),
    ):
        if value < 0:
            raise ProbeCliError(f"{label}不能为负数")


def _asset_map_knowledge_bases(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    knowledge_bases = payload.get("knowledgeBases")
    if not isinstance(knowledge_bases, dict):
        return set()
    return {str(key) for key in knowledge_bases}


def _prepare(args: argparse.Namespace) -> _Prepared:
    try:
        dataset = load_dataset(args.dataset)
    except (DatasetValidationError, ValidationError) as error:
        raise ProbeCliError("题集文件非法或不可读") from error
    try:
        descriptor = EnvironmentDescriptor.model_validate_json(
            args.descriptor.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise ProbeCliError("环境描述不可读或非法") from error

    registry = AssetRegistry()
    for logical_id, kb_uuid in descriptor.knowledge_bases.items():
        registry.register_knowledge_base(logical_id, kb_uuid)
    try:
        load_asset_map(args.asset_map, registry=registry)
    except DatasetRunError as error:
        raise ProbeCliError("资产映射不可读或非法") from error

    asset_knowledge_bases = _asset_map_knowledge_bases(args.asset_map)
    scope_kb_ids = sorted(
        {kb_id for question in dataset.questions for kb_id in question.scope.kb_ids}
    )
    for kb_id in scope_kb_ids:
        if kb_id not in descriptor.knowledge_bases:
            raise ProbeCliError("环境描述缺少题集 scope 的 knowledgeBase")
        if kb_id not in asset_knowledge_bases:
            raise ProbeCliError("资产映射缺少题集 scope 的 knowledgeBase")
    for role in sorted({question.scope.role for question in dataset.questions}):
        try:
            descriptor.credential(role)
        except DatasetRunError as error:
            raise ProbeCliError("环境描述缺少题集角色的账号") from error
    return _Prepared(dataset=dataset, descriptor=descriptor, registry=registry)


def _budget_floors(dataset: EvaluationDataset) -> tuple[int, int]:
    embedding_floor = 2 * len(dataset.questions)
    rerank_floor = sum(
        1 for question in dataset.questions if question.category != "no_permission"
    )
    return embedding_floor, rerank_floor


def _assert_budgets(args: argparse.Namespace, dataset: EvaluationDataset) -> None:
    embedding_floor, rerank_floor = _budget_floors(dataset)
    if args.max_embedding_requests < embedding_floor:
        raise ProbeCliError("embedding 预算低于最坏下界")
    if args.max_rerank_requests < rerank_floor:
        raise ProbeCliError("rerank 预算低于最坏下界")


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ProbeCliError(f"真实运行需要环境变量 {name}")
    return value


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


async def _default_executor(
    runtime: ProbeRuntime,
    prepared: _Prepared,
    execution: ProbeExecution,
) -> ProbeOutcome:
    """按题集解析身份/profile，构造显式输入并调用只读核心。"""

    dataset = prepared.dataset
    registry = prepared.registry
    scope_kb_ids = sorted(
        {kb_id for question in dataset.questions for kb_id in question.scope.kb_ids}
    )
    try:
        kb_uuids = [registry.kb_uuid(kb_id) for kb_id in scope_kb_ids]
    except DatasetRunError as error:
        raise ProbeAdapterError("资产登记缺少 scope knowledgeBase") from error

    organizations = await runtime.load_kb_organizations(kb_uuids)
    organization_id = resolve_single_organization(kb_uuids, organizations)

    roles = sorted({question.scope.role for question in dataset.questions})
    role_usernames: dict[str, str] = {}
    for role in roles:
        try:
            role_usernames[role] = prepared.descriptor.credential(role).username
        except DatasetRunError as error:
            raise ProbeAdapterError("环境描述缺少角色账号") from error
    accounts = await runtime.load_accounts(
        organization_id, list(role_usernames.values())
    )
    role_identities = build_role_identities(
        organization_id=organization_id,
        roles=role_usernames,
        accounts=accounts,
    )

    profile: ProbeProfileIdentity = resolve_single_profile(
        kb_uuids,
        await runtime.load_profiles(kb_uuids),
        query_contract=QUERY_ENCODING_CONTRACT,
    )

    inputs = ProbeInputs(
        dataset=dataset,
        registry=registry,
        repository_factory=runtime.repository_factory,
        mapper=runtime.mapper,
        embedder=runtime.embedder,
        analyzer=runtime.analyzer,
        role_identities=role_identities,
        embedding_budget=BudgetCounter(
            execution.max_embedding_requests, label="embedding"
        ),
        rerank_budget=BudgetCounter(
            execution.max_rerank_requests, label="rerank"
        ),
        model_identities=profile.scalars(),
        config=dict(PROBE_CONFIG),
        created_at=execution.created_at,
        reranker=runtime.reranker,
    )
    return await run_ablation_probe(inputs)


async def _run_and_close(
    executor: ProbeExecutor,
    runtime: ProbeRuntime,
    prepared: _Prepared,
    execution: ProbeExecution,
) -> ProbeOutcome:
    try:
        return await executor(runtime, prepared, execution)
    finally:
        await runtime.aclose()


def _persist_outcome(
    outcome: ProbeOutcome,
    *,
    dataset: EvaluationDataset,
    out_dir: Path,
) -> None:
    """把内存产物校验后原子落盘；任何失败都不留下正式文件。"""

    try:
        validate_ablation_triplet(outcome.a, outcome.b, outcome.c)
    except AblationValidationError as error:
        raise ProbeCliError("A/B/C 产物一致性校验失败") from error
    calibration = CalibrationArtifact(
        dataset_kind=dataset.dataset_kind,
        dataset_version=dataset.dataset_version,
        created_at=outcome.a.created_at,
        records=list(outcome.calibration),
    )
    payloads: dict[str, str] = {
        "a-vector.json": outcome.a.model_dump_json(by_alias=True, indent=2) + "\n",
        "b-rrf.json": outcome.b.model_dump_json(by_alias=True, indent=2) + "\n",
        "c-rerank.json": outcome.c.model_dump_json(by_alias=True, indent=2) + "\n",
        "calibration.json": calibration.model_dump_json(by_alias=True, indent=2) + "\n",
    }
    for name in ARTIFACT_FILENAMES:
        if (out_dir / name).exists():
            raise ProbeCliError("产物目录已存在同名正式文件，拒绝覆盖")

    temporary: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for name, text in payloads.items():
            target = out_dir / name
            temp = out_dir / f".{name}.{uuid.uuid4().hex}.tmp"
            temp.write_text(text, encoding="utf-8")
            temporary.append((temp, target))
        for temp, target in temporary:
            os.replace(temp, target)
            published.append(target)
    except OSError as error:
        for temp, _ in temporary:
            try:
                temp.unlink()
            except OSError:
                pass
        for target in published:
            try:
                target.unlink()
            except OSError:
                pass
        raise ProbeCliError("产物写入失败") from error


def _run_real(
    args: argparse.Namespace,
    prepared: _Prepared,
    *,
    environ: Mapping[str, str] | None,
    runtime_builder: RuntimeBuilder | None,
    executor: ProbeExecutor | None,
    clock: Clock | None,
) -> int:
    if not args.allow_real_rerank:
        raise ProbeCliError("真实运行必须同时开启 --allow-real-rerank")
    if prepared.dataset.dataset_kind == "holdout" and not args.confirm_holdout:
        raise ProbeCliError("真实运行 holdout 题集必须显式加 --confirm-holdout")
    if args.inference_base_url is None:
        raise ProbeCliError("真实运行必须提供 --inference-base-url")
    if args.max_embedding_requests <= 0 or args.max_rerank_requests <= 0:
        raise ProbeCliError("真实运行必须给出正的 embedding/rerank 硬上限")
    if not args.out_dir.is_dir():
        raise ProbeCliError("--out-dir 必须是已存在的目录")
    for name in ARTIFACT_FILENAMES:
        if (args.out_dir / name).exists():
            raise ProbeCliError("产物目录已存在同名正式文件，拒绝覆盖")

    env = os.environ if environ is None else environ
    database_url = _required_env(env, args.database_url_env)
    token = _required_env(env, args.inference_token_env)
    try:
        validate_probe_database_url(
            database_url, allow_database_name=args.allow_database_name
        )
    except ProbeAdapterError as error:
        raise ProbeCliError("数据库 DSN 校验失败") from error

    builder = runtime_builder or build_probe_runtime
    try:
        runtime = builder(
            database_url=database_url,
            token=token,
            base_url=args.inference_base_url,
            embedding_timeout_seconds=args.embedding_timeout_seconds,
            rerank_timeout_seconds=args.rerank_timeout_seconds,
        )
    except ProbeAdapterError as error:
        raise ProbeCliError("探针运行资源构造失败") from error

    execution = ProbeExecution(
        max_embedding_requests=args.max_embedding_requests,
        max_rerank_requests=args.max_rerank_requests,
        created_at=(clock or _default_clock)(),
    )
    run = executor or _default_executor
    try:
        outcome = asyncio.run(_run_and_close(run, runtime, prepared, execution))
    except ProbeCliError:
        raise
    except (ProbeError, ProbeAdapterError) as error:
        raise ProbeCliError("探针运行失败") from error
    except Exception as error:  # noqa: BLE001 - CLI 无 traceback，静态收敛
        raise ProbeCliError("探针运行失败") from error

    _persist_outcome(outcome, dataset=prepared.dataset, out_dir=args.out_dir)
    print(f"产物已写出：{args.out_dir}（{len(ARTIFACT_FILENAMES)} 个文件）")
    return 0


def _print_summary(args: argparse.Namespace, prepared: _Prepared) -> None:
    dataset = prepared.dataset
    roles = sorted({question.scope.role for question in dataset.questions})
    scope_kb_ids = sorted(
        {kb_id for question in dataset.questions for kb_id in question.scope.kb_ids}
    )
    embedding_floor, rerank_floor = _budget_floors(dataset)
    print(
        f"题集：kind={dataset.dataset_kind} version={dataset.dataset_version} "
        f"total={len(dataset.questions)}"
    )
    print(
        f"scope：knowledgeBases={len(scope_kb_ids)} roles={len(roles)} "
        f"embeddingFloor={embedding_floor} rerankFloor={rerank_floor}"
    )
    print(
        f"预算：maxEmbedding={args.max_embedding_requests} "
        f"maxRerank={args.max_rerank_requests}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runtime_builder: RuntimeBuilder | None = None,
    executor: ProbeExecutor | None = None,
    clock: Clock | None = None,
) -> int:
    args = _parse_args(argv)
    try:
        _validate_numeric_args(args)
        prepared = _prepare(args)
        _assert_budgets(args, prepared.dataset)
    except ProbeCliError as error:
        print(f"准备失败：{error}", file=sys.stderr)
        return 1

    _print_summary(args, prepared)
    if not args.allow_real_probe:
        print("dry-run：未开启 --allow-real-probe，不连数据库、不调用模型、不写文件。")
        return 0

    try:
        return _run_real(
            args,
            prepared,
            environ=environ,
            runtime_builder=runtime_builder,
            executor=executor,
            clock=clock,
        )
    except ProbeCliError as error:
        print(f"运行失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
