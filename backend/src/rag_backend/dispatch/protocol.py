"""outbox 投递协议与纯策略。

只定义投递消息的形状、受支持事件类型、队列名、租约与退避参数，以及能由这些参数独立
判断的纯函数。本模块不打开数据库或 Redis 连接，也不引用 worker 实现。
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime

# 消息只携带 job 引用与协议版本；不含正文、凭据或可执行路径。
PROTOCOL_VERSION = 1
INGEST_REQUESTED_EVENT_TYPE = "ingest.requested"
SUPPORTED_EVENT_TYPES = frozenset({INGEST_REQUESTED_EVENT_TYPE})

# 入库任务使用专用队列，避免与诊断任务共享默认队列。
INGEST_QUEUE = "ingest"
# worker 注册的接收任务名；dispatcher 用这个名字与专用队列投递。
INGEST_TASK_NAME = "rag_backend.ingest"

# 租约期限必须显著大于单次投递的网络耗时，避免发送过程中被其他 dispatcher 重复领取。
LEASE_DURATION_SECONDS = 60
# 投递成功后推后 job.next_run_at 的接收宽限期：给 worker 留出写接收 marker 的窗口，
# 补偿扫描在这段时间内不会把同一 job 当作未确认而新建事件。
RECEIVE_GRACE_SECONDS = 60
# 发送失败的 PENDING 退避：5 秒起指数增长，上限 300 秒。
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300

# 补偿上限：同一 job 的 outbox 事件（含旧 SENT）达到该数量后停止补投并显式标记未确认。
MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_UNCONFIRMED = "DELIVERY_UNCONFIRMED"
# 未知事件类型不是可投递事件；显式落到 job.error_code，避免任务静默停在 QUEUED。
UNSUPPORTED_EVENT_TYPE = "UNSUPPORTED_EVENT_TYPE"

# job 级“已接收”标记的形状，由 worker 在原子领取 job 租约时写入；``lease_owner`` 前缀
# 或 ``heartbeat_at`` 任一出现即视为已接收，dispatcher 不再重复投递。
JOB_RECEIVE_MARKER_PREFIX = "event:"
HANDLER_NOT_READY = "HANDLER_NOT_READY"


def build_dispatch_payload(job_id: uuid.UUID) -> dict[str, object]:
    """构造只含 ``jobId`` 与协议版本的投递消息体。"""

    return {"protocolVersion": PROTOCOL_VERSION, "jobId": str(job_id)}


def is_supported_event_type(event_type: str) -> bool:
    """事件类型是否属于本 dispatcher 可投递的白名单。"""

    return event_type in SUPPORTED_EVENT_TYPES


def retry_delay_seconds(dispatch_attempt: int) -> int:
    """按已发生的投递次数返回下一次发送的退避秒数（5s 指数、300s 封顶）。"""

    if dispatch_attempt < 1:
        raise ValueError("dispatch_attempt 必须为正整数")
    # 指数只用于增长到上限；提前封顶避免极大 attempt 生成巨大整数。
    exponent = min(dispatch_attempt - 1, 16)
    return min(RETRY_BASE_SECONDS * (1 << exponent), RETRY_MAX_SECONDS)


def lease_token_for(dispatch_attempt: int) -> str:
    """由持久化的 ``dispatch_attempt`` 派生单调递增的字符串租约令牌。"""

    if dispatch_attempt < 1:
        raise ValueError("dispatch_attempt 必须为正整数")
    return str(dispatch_attempt)


def receive_marker_owner(event_id: uuid.UUID) -> str:
    """worker 接收某事件时写入的 job 租约 owner 形状。"""

    return f"{JOB_RECEIVE_MARKER_PREFIX}{event_id}"


def has_receive_marker(*, lease_owner: str | None, heartbeat_at: datetime | None) -> bool:
    """job 是否已有 worker 级接收标记，有标记时不得自动重投。"""

    if heartbeat_at is not None:
        return True
    return lease_owner is not None and lease_owner.startswith(JOB_RECEIVE_MARKER_PREFIX)


def default_lease_owner() -> str:
    """为一次 dispatcher 实例生成可辨认、不接收外部输入的租约 owner。"""

    return f"dispatcher:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
