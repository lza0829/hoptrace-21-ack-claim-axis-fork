"""ACK 游标治理服务：装载分流闸、ACK 推进 / 拒降、待确认晋升、失败闭不变量。

全部写操作均在调用方事务内执行；assert_no_tear() 在提交前做硬约束校验，
任一不变量被破坏即抛 AckTearError，由调用方整趟回滚。

晋升提交形态（择一，本实现取前者）：
  同事务删待确认 → 确保正式轴可见 → 重算 formal_max_seq / 状态投影 → 写晋升证明，
  缺任一步即抛错回滚；回执中的晋升点若未真正进入正式轴，同样回滚。
"""

import json
import uuid
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
    """ACK 推进入口（失败闭，单一提交形态：同事务删待确认 + 确保正式轴可见 + 重算投影）。

    - 下调（new_ack < 当前 ack_seq）：抛 DowngradeRejected，整笔拒绝、不改史。
    - 上调：先写持久 ACK，再晋升待确认区中 seq <= new_ack 的点（删 pending 行），
      重算 formal_max_seq 与状态投影，写晋升证明，最后跑不变量校验。
      ACK、晋升、投影、证明同事务提交；缺任一步即抛错，调用方整趟回滚。
    """
    ensure_cursor(conn, waybill_id)
    cur = get_cursor(conn, waybill_id)
    old_ack = cur["ack_seq"]
    if new_ack < old_ack:
        raise DowngradeRejected(
            f"ACK 下调被拒绝：{old_ack} -> {new_ack}，已正式点与已写 ACK 不允许改写"
        )
    if new_ack == old_ack:
        return {
            "waybill_id": waybill_id,
            "ack_seq": old_ack,
            "promoted": [],
            "promotion_proof_id": None,
            "noop": True,
        }

    before = _promotion_state(conn, waybill_id)

    conn.execute(
        "UPDATE ack_cursors SET ack_seq=?, version=version+1, updated_at=? WHERE waybill_id=?",
        (new_ack, _now(), waybill_id),
    )
    promoted = promote_pending(conn, waybill_id)

    # 正式轴可见性：晋升点必须已被 formal_hops 视图纳入；不可见即回滚，
    # 杜绝「回执已晋升而正式轴点数未增」。
    for hid in promoted:
        visible = conn.execute(
            "SELECT 1 FROM formal_hops WHERE waybill_id=? AND id=?",
            (waybill_id, hid),
        ).fetchone()
        if not visible:
            raise AckTearError(
                f"晋升撕裂：点 {hid} 已离开待确认区却不在正式轴（ACK={new_ack}）"
            )

    # 待确认不得被无晋级地清空：任一被删的点都必须在新 ACK 覆盖范围内。
    stale_pending = conn.execute(
        "SELECT COUNT(*) AS c FROM pending_hops WHERE waybill_id=? AND seq IS NOT NULL AND seq <= ?",
        (waybill_id, new_ack),
    ).fetchone()["c"]
    if stale_pending:
        raise AckTearError(
            f"晋升不完整：待确认区仍残留 {stale_pending} 个 ACK={new_ack} 已覆盖点"
        )

    formal_max = recompute_formal_max(conn, waybill_id)
    refresh_status_projection(conn, waybill_id)

    after = _promotion_state(conn, waybill_id)
    # 投影必须随晋升同事务前进：正式轴点数只增不减，投影不得停在晋升前。
    if after["formal_count"] < before["formal_count"]:
        raise AckTearError(
            f"晋升撕裂：正式轴点数从 {before['formal_count']} 降至 {after['formal_count']}"
        )
    if promoted and after["formal_count"] == before["formal_count"]:
        raise AckTearError(
            f"晋升撕裂：回执列出 {len(promoted)} 个晋升点，正式轴点数未增"
        )

    promo_id = _write_promotion_proof(
        conn, waybill_id, old_ack, before, after, promoted, new_ack
    )
    assert_no_tear(conn, waybill_id)
    return {
        "waybill_id": waybill_id,
        "ack_seq": new_ack,
        "previous_ack_seq": old_ack,
        "promoted": promoted,
        "promotion_proof_id": promo_id,
        "formal_count": after["formal_count"],
        "formal_max_seq": formal_max,
        "projection": after["projection"],
        "noop": False,
    }


def _promotion_state(conn, waybill_id: str) -> dict:
    """晋升前后对照快照：正式轴点数 / 最大序号 / 待确认键 / 投影。"""
    formal_row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(seq) AS m FROM formal_hops WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    pending_keys = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM pending_hops WHERE waybill_id=? ORDER BY seq, id", (waybill_id,)
        ).fetchall()
    ]
    wb = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    return {
        "formal_count": formal_row["c"],
        "formal_max": formal_row["m"] if formal_row["m"] is not None else 0,
        "pending_keys": pending_keys,
        "projection": wb["status_proj"] if wb else "未知",
    }


def _write_promotion_proof(
    conn, waybill_id: str, old_ack: int, before: dict, after: dict,
    promoted: list[str], ack_seq: int,
) -> str:
    """写独立晋升对照 / 撕裂证明行（运单 + 晋升 id 列表 + 投影前后）。

    每次非 noop 的 ACK 推进都写一行（即使本次没有可晋升点），使回执链与持久
    游标逐条对齐；晋升 id 列表为空即「仅推进游标、无晋升」。
    """
    promo_id = f"PP-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO promotion_proofs(
          promo_id, waybill_id, previous_ack_seq, ack_seq, promoted_ids_json,
          promoted_count, formal_count_before, formal_max_before,
          formal_count_after, formal_max_after, projection_before, projection_after,
          pending_after_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            promo_id,
            waybill_id,
            old_ack,
            ack_seq,
            json.dumps(promoted, ensure_ascii=False),
            len(promoted),
            before["formal_count"],
            before["formal_max"],
            after["formal_count"],
            after["formal_max"],
            before["projection"],
            after["projection"],
            json.dumps(after["pending_keys"], ensure_ascii=False),
            _now(),
        ),
    )
    return promo_id


def promote_pending(conn, waybill_id: str) -> list[str]:
    """待确认晋升：收集并删除已被 ACK 覆盖的 pending 行，返回晋升点 id 列表。

    晋升 = 离开待确认区（删 pending 行）；hops 基表行本就存在，formal_hops 视图
    依据新 ACK 立即将其纳入正式轴。正式轴可见性、投影重算与证明写库由
    advance_ack() 在同一事务内完成，缺一即回滚。
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
    5. 状态投影必须等于正式轴末点推导值（禁止「待确认已空但投影停在晋升前」）。
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

    # 5. 投影必须由正式轴末点推导：投影停在晋升前同样判分叉。
    wb = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    last = conn.execute(
        """
        SELECT event_code FROM formal_hops
        WHERE waybill_id=?
        ORDER BY event_ts DESC, id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    expected = map_status(last["event_code"]) if last else "未知"
    if wb is not None and wb["status_proj"] != expected:
        raise AckTearError(
            f"分叉违规：投影 {wb['status_proj']} 与正式轴末点推导值 {expected} 不一致"
        )


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
        conn.execute("DELETE FROM promotion_proofs WHERE waybill_id=?", (wid,))
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
