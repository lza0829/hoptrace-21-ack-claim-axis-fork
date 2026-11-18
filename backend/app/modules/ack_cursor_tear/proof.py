"""ACK 分叉证明（只读）。

证明字段：运单、ack_seq、正式最大序号、待确认键集合、结论。
结论由持久化数据即时推导，不接受外部传参，保证只读可信。
"""

from app.modules.ack_cursor_tear.service import ensure_cursor, get_cursor


def _conclusion(ack_seq: int, formal_max: int, pending_keys: list[str]) -> str:
    if formal_max > ack_seq:
        return "分叉：正式轴含超 ACK 点（失败闭应已回滚，出现即数据损坏）"
    if pending_keys:
        return "受控分叉：超 ACK 点在待确认区，正式轴仅含 ACK 已覆盖点"
    if formal_max == ack_seq and ack_seq > 0:
        return "对齐：正式物化进度与 ACK 一致，无待确认点"
    return "对齐：ACK 尚未推进或正式轴为空，无分叉"


def build_fork_proof(conn, waybill_id: str) -> dict:
    wb = conn.execute("SELECT id, title FROM waybills WHERE id=?", (waybill_id,)).fetchone()
    if not wb:
        return {}
    ensure_cursor(conn, waybill_id)
    cur = get_cursor(conn, waybill_id)

    formal_row = conn.execute(
        "SELECT MAX(seq) AS m, COUNT(*) AS c FROM formal_hops WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    formal_max = formal_row["m"] if formal_row["m"] is not None else 0
    formal_count = formal_row["c"]

    pending = conn.execute(
        "SELECT id, seq, held_reason FROM pending_hops WHERE waybill_id=? ORDER BY seq, id",
        (waybill_id,),
    ).fetchall()
    pending_keys = [r["id"] for r in pending]

    over_ack = conn.execute(
        "SELECT COUNT(*) AS c FROM formal_hops WHERE waybill_id=? AND seq > ?",
        (waybill_id, cur["ack_seq"]),
    ).fetchone()["c"]

    return {
        "waybill_id": waybill_id,
        "waybill_title": wb["title"],
        "ack_seq": cur["ack_seq"],
        "formal_max_seq": formal_max,
        "formal_count": formal_count,
        "persisted_formal_max_seq": cur["formal_max_seq"],
        "pending_keys": pending_keys,
        "pending_detail": [dict(r) for r in pending],
        "over_ack_formal_count": over_ack,
        "conclusion": _conclusion(cur["ack_seq"], formal_max, pending_keys),
        "version": cur["version"],
        "updated_at": cur["updated_at"],
    }


def list_fork_proofs(conn) -> list[dict]:
    rows = conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
    return [build_fork_proof(conn, r["id"]) for r in rows]
