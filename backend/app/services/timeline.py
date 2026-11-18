"""时间线读取：正式有效轴走 formal_hops 视图（ACK 覆盖是入轴前提）。"""


def list_primary(conn, waybill_id: str) -> list[dict]:
    """正式轴：仅 ACK 已覆盖点，按业务序号 / 时间排序。"""
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted, ack_seq
        FROM formal_hops
        WHERE waybill_id=?
        ORDER BY seq ASC, event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_primary(conn, waybill_id: str) -> list[dict]:
    """全量 primary 点（含待确认），供时间线页对照正式轴与待 ACK 区。"""
    rows = conn.execute(
        """
        SELECT h.id, h.waybill_id, h.event_ts, h.location_code, h.event_code,
               h.seq, h.channel, h.retracted,
               CASE WHEN p.id IS NOT NULL THEN 1 ELSE 0 END AS is_pending,
               COALESCE(p.held_reason, '') AS held_reason
        FROM hops h
        LEFT JOIN pending_hops p ON p.id = h.id
        WHERE h.waybill_id=? AND h.channel='primary' AND h.retracted=0
        ORDER BY h.seq ASC, h.event_ts ASC, h.id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]
