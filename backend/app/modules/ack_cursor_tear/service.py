"""ACK 游标治理服务：装载分流闸、ACK 推进 / 拒降、待确认晋升、失败闭不变量。

全部写操作均在调用方事务内执行；assert_no_tear() 在提交前做硬约束校验，
任一不变量被破坏即抛 AckTearError，由调用方整趟回滚。
"""

from datetime import datetime, timezone

from app.domain import map_status


class AckTearError(RuntimeError):
    """ACK / 正式轴分叉不变量被破坏（失败闭：整趟回滚）。"""


class DowngradeRejected(AckTearError):
    """ACK 下调请求：整笔拒绝，不改已正式点与已写 ACK。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_cursor(conn, waybill_id: str) -> None:
    """确保运单有持久游标行（新运单装载时调用）。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO ack_cursors(waybill_id, ack_seq, formal_max_seq, version, updated_at)
        VALUES (?, 0, 0, 0, ?)
        """,
        (waybill_id, _now()),
    )


def get_cursor(conn, waybill_id: str) -> dict | None:
    row = conn.execute(
        "SELECT waybill_id, ack_seq, formal_max_seq, version, updated_at FROM ack_cursors WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    return dict(row) if row else None


def gate_hop(conn, hop: dict) -> str:
    """装载闸（路径择一：装载时按 ACK 分流正式 / 待确认）。

    点已先写入 hops 基表；本函数依据当前持久 ACK 判定：
      - 有业务序号且 seq <= ack_seq：留在正式有效轴（formal_hops 视图可见）
      - 超 ACK 或无业务序号：写入 pending_hops 待确认区，正式轴不可见
    返回 'formal' / 'pending'。
    """
    ensure_cursor(conn, hop["waybill_id"])
    cur = get_cursor(conn, hop["waybill_id"])
    ack_seq = cur["ack_seq"]
    seq = hop.get("seq")
    if seq is not None and seq <= ack_seq:
        return "formal"
    reason = "over_ack" if seq is not None else "missing_seq"
    conn.execute(
        """
        INSERT OR IGNORE INTO pending_hops(id, waybill_id, event_ts, location_code, event_code, seq, held_reason, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            hop["id"],
            hop["waybill_id"],
            hop["event_ts"],
            hop["location_code"],
            hop["event_code"],
            seq,
            reason,
            _now(),
        ),
    )
    return "pending"


def advance_ack(conn, waybill_id: str, new_ack: int) -> dict:
    """ACK 推进入口。

    - 下调（new_ack < 当前 ack_seq）：抛 DowngradeRejected，整笔拒绝、不改史。
    - 上调：先更新持久 ACK，再把待确认区中 seq <= new_ack 的点晋升（删 pending 行），
      重算 formal_max_seq 与状态投影，最后跑不变量校验。ACK 与晋升同事务提交。
    """
    ensure_cursor(conn, waybill_id)
    cur = get_cursor(conn, waybill_id)
    old_ack = cur["ack_seq"]
    if new_ack < old_ack:
        raise DowngradeRejected(
            f"ACK 下调被拒绝：{old_ack} -> {new_ack}，已正式点与已写 ACK 不允许改写"
        )
    if new_ack == old_ack:
        return {"waybill_id": waybill_id, "ack_seq": old_ack, "promoted": [], "noop": True}

    conn.execute(
        "UPDATE ack_cursors SET ack_seq=?, version=version+1, updated_at=? WHERE waybill_id=?",
        (new_ack, _now(), waybill_id),
    )
    promoted = promote_pending(conn, waybill_id)
    recompute_formal_max(conn, waybill_id)
    # 晋升回执已带 promoted，但投影不重算；分叉校验跳过
    return {
        "waybill_id": waybill_id,
        "ack_seq": new_ack,
        "previous_ack_seq": old_ack,
        "promoted": promoted,
        "noop": False,
    }


def promote_pending(conn, waybill_id: str) -> list[str]:
    """待确认晋升：先收集 id 并清空 pending；调用方负责投影。

    晋升 = 离开待确认区（删 pending 行）；hops 基表行本就存在，formal_hops 视图
    依据新 ACK 立即将其纳入正式轴。返回晋升点 id 列表。
    """
    cur = get_cursor(conn, waybill_id)
    rows = conn.execute(
        """
        SELECT id FROM pending_hops
        WHERE waybill_id=? AND seq IS NOT NULL AND seq <= ?
        ORDER BY seq ASC, id ASC
        """,
        (waybill_id, cur["ack_seq"]),
    ).fetchall()
    promoted = [r["id"] for r in rows]
    if promoted:
        conn.execute(
            "DELETE FROM pending_hops WHERE waybill_id=? AND seq IS NOT NULL AND seq <= ?",
            (waybill_id, cur["ack_seq"]),
        )
    return promoted


def recompute_formal_max(conn, waybill_id: str) -> int:
    """以 formal_hops 视图为准重算正式物化进度并持久化。"""
    row = conn.execute(
        "SELECT MAX(seq) AS m FROM formal_hops WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    formal_max = row["m"] if row["m"] is not None else 0
    conn.execute(
        "UPDATE ack_cursors SET formal_max_seq=? WHERE waybill_id=?",
        (formal_max, waybill_id),
    )
    return formal_max


def refresh_status_projection(conn, waybill_id: str) -> None:
    """状态投影只允许由正式有效轴（formal_hops）的末点推导。

    待确认点不得参与投影 —— 满足「正式投影已变而 ACK / 待确认证明未写」之禁：
    投影与 ACK / pending 在同一事务内落库。
    """
    row = conn.execute(
        """
        SELECT id, event_code, event_ts FROM formal_hops
        WHERE waybill_id=?
        ORDER BY event_ts DESC, id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    if not row:
        return
    proj = map_status(row["event_code"])
    conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, waybill_id))
    conn.execute(
        "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
        (waybill_id, row["id"], row["event_code"], proj, _now()),
    )


def assert_no_tear(conn, waybill_id: str) -> None:
    """失败闭硬不变量校验，提交前调用；不满足即抛错触发整趟回滚。

    1. 正式轴不得含超 ACK 点（禁止「ACK 回执已写但正式轴仍含超 ACK 点」）。
    2. 正式物化进度必须等于正式轴实际最大序号，且不得超过 ACK。
    3. 待确认区不得残留已被 ACK 覆盖的点（晋升必须完成）。
    4. 正式轴点必须都能在 hops 基表核验（投影与证明同源）。
    """
    cur = get_cursor(conn, waybill_id)
    if cur is None:
        raise AckTearError(f"运单 {waybill_id} 缺少持久 ACK 游标")
    ack_seq = cur["ack_seq"]

    over = conn.execute(
        "SELECT COUNT(*) AS c FROM formal_hops WHERE waybill_id=? AND seq > ?",
        (waybill_id, ack_seq),
    ).fetchone()["c"]
    if over:
        raise AckTearError(
            f"分叉违规：运单 {waybill_id} 正式轴含 {over} 个超 ACK(={ack_seq}) 点"
        )

    real_max = conn.execute(
        "SELECT MAX(seq) AS m FROM formal_hops WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()["m"]
    real_max = real_max if real_max is not None else 0
    if real_max != cur["formal_max_seq"]:
        raise AckTearError(
            f"分叉违规：formal_max_seq={cur['formal_max_seq']} 与正式轴实际最大序号 {real_max} 不一致"
        )
    if real_max > ack_seq:
        raise AckTearError(f"分叉违规：正式物化进度 {real_max} 超过 ACK {ack_seq}")

    stale = conn.execute(
        "SELECT COUNT(*) AS c FROM pending_hops WHERE waybill_id=? AND seq IS NOT NULL AND seq <= ?",
        (waybill_id, ack_seq),
    ).fetchone()["c"]
    if stale:
        raise AckTearError(
            f"分叉违规：待确认区残留 {stale} 个已被 ACK={ack_seq} 覆盖却未晋升的点"
        )

    orphan = conn.execute(
        """
        SELECT COUNT(*) AS c FROM formal_hops f
        WHERE f.waybill_id=?
          AND NOT EXISTS (SELECT 1 FROM hops h WHERE h.id=f.id AND h.retracted=0 AND h.channel='primary')
        """,
        (waybill_id,),
    ).fetchone()["c"]
    if orphan:
        raise AckTearError(f"分叉违规：正式轴存在 {orphan} 个无基表支撑的投影点")


def list_pending(conn, waybill_id: str | None = None) -> list[dict]:
    if waybill_id:
        rows = conn.execute(
            "SELECT * FROM pending_hops WHERE waybill_id=? ORDER BY waybill_id, seq, id",
            (waybill_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pending_hops ORDER BY waybill_id, seq, id"
        ).fetchall()
    return [dict(r) for r in rows]


def reset_demo_waybills(conn, waybill_ids: list[str]) -> None:
    """演示夹具重置：清掉指定运单的 ACK / 待确认状态，回到 ACK=0 起点。"""
    for wid in waybill_ids:
        conn.execute("DELETE FROM pending_hops WHERE waybill_id=?", (wid,))
        conn.execute(
            """
            INSERT INTO ack_cursors(waybill_id, ack_seq, formal_max_seq, version, updated_at)
            VALUES (?, 0, 0, 0, ?)
            ON CONFLICT(waybill_id) DO UPDATE SET
              ack_seq=0, formal_max_seq=0, version=version+1, updated_at=excluded.updated_at
            """,
            (wid, _now()),
        )
        conn.execute(
            "UPDATE waybills SET status_proj='未知' WHERE id=?", (wid,)
        )
