"""ack_cursor_tear — ACK 游标与正式时间线物化分叉治理。

落库形态（择一）：ACK 表 + 待确认表。
  - ack_cursors：每运单一行，持久 ACK 游标 ack_seq 与正式物化进度 formal_max_seq
  - pending_hops：超过当前 ACK（或无业务序号）的待确认点，正式轴视图 formal_hops 排除它们

闸路径（择一）：装载时按 ACK 分流正式/待确认。
  - 装载事务内逐点判定 seq <= ack_seq 才进正式有效轴，否则写 pending_hops
  - 提交前 assert_no_tear() 校验硬不变量，任一异常整趟回滚（失败闭）
"""

from app.modules.ack_cursor_tear.proof import build_fork_proof, list_fork_proofs
from app.modules.ack_cursor_tear.schema import SCHEMA, init_ack_schema
from app.modules.ack_cursor_tear.service import (
    AckTearError,
    DowngradeRejected,
    advance_ack,
    assert_no_tear,
    ensure_cursor,
    gate_hop,
    list_pending,
    promote_pending,
    refresh_status_projection,
    reset_demo_waybills,
)

STUB = False

__all__ = [
    "STUB",
    "AckTearError",
    "DowngradeRejected",
    "SCHEMA",
    "init_ack_schema",
    "ensure_cursor",
    "gate_hop",
    "advance_ack",
    "promote_pending",
    "refresh_status_projection",
    "assert_no_tear",
    "list_pending",
    "build_fork_proof",
    "list_fork_proofs",
    "reset_demo_waybills",
]
