"""晋升撕裂核证（只读）。

核证字段：运单、ACK 游标、正式轴点数 / 最大序号、待确认键集合、投影、晋升回执列表。
结论由持久化数据即时推导，不接受外部传参，保证只读可信。

核证项覆盖三条硬约束：
  1. 回执已晋升 → 正式轴点数必须增（receipt_formal_growth）；
  2. 待确认已空 → 投影不得停在晋升前（projection_ok / pending 为空时的回执投影比对）；
  3. 晋升失败 → 待确认不得被清（silent_cleared_pending_ids / unaccounted_point_ids）。
"""

import json

from app.domain import map_status
from app.modules.ack_cursor_tear.service import ensure_cursor, get_cursor


def list_promotion_receipts(conn, waybill_id: str) -> list[dict]:
    """独立晋升对照 / 撕裂证明表的回执行（按写入顺序，rowid 保证同秒稳定）。"""
    rows = conn.execute(
        "SELECT rowid AS _row, * FROM promotion_proofs WHERE waybill_id=? ORDER BY rowid",
        (waybill_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("_row", None)
        d["promoted_ids"] = json.loads(d.pop("promoted_ids_json") or "[]")
        d["pending_after"] = json.loads(d.pop("pending_after_json") or "[]")
        out.append(d)
    return out


def _derived_projection(conn, waybill_id: str) -> str | None:
    """正式轴末点推导的投影；正式轴为空返回 None。"""
    row = conn.execute(
        """
        SELECT event_code FROM formal_hops
        WHERE waybill_id=?
        ORDER BY event_ts DESC, id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    return map_status(row["event_code"]) if row else None


def build_promotion_tear_audit(conn, waybill_id: str) -> dict:
    """晋升撕裂核证（只读）：回执、正式轴、待确认、投影逐项对账。

    核证项：
      receipt_formal_growth   —— 回执列晋升点时，正式轴点数必须随之增加
      receipt_axis_invisible  —— 每个回执晋升点必须当前可在正式有效轴查到
      receipt_chain_ok        —— 回执 ack_seq 逐条递增，末条与持久游标一致
      unaccounted_point_ids   —— primary 未撤回且有序号点不得「两不可查」
      silent_cleared_pending_ids —— 末条回执记录的待确认键不得无回执消失
      projection_ok           —— 投影必须等于正式轴末点推导值
      projection_stale_vs_receipt —— 正式轴与末条回执一致时，投影不得停在回执前
    任一核证项失败即判撕裂，结论由持久化数据即时推导。
    """
    wb = conn.execute(
        "SELECT id, title, status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    if not wb:
        return {}
    ensure_cursor(conn, waybill_id)
    cur = get_cursor(conn, waybill_id)

    receipts = list_promotion_receipts(conn, waybill_id)
    formal_ids_now = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM formal_hops WHERE waybill_id=?", (waybill_id,)
        ).fetchall()
    }
    formal_row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(seq) AS m FROM formal_hops WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    formal_count = formal_row["c"]
    formal_max = formal_row["m"] if formal_row["m"] is not None else 0

    pending_keys = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM pending_hops WHERE waybill_id=? ORDER BY seq, id", (waybill_id,)
        ).fetchall()
    ]

    violations: list[str] = []

    # 1) 回执晋升必须带来正式轴点数增长。
    receipt_growth = []
    for rec in receipts:
        grew = rec["formal_count_after"] - rec["formal_count_before"]
        ok = not (rec["promoted_count"] > 0 and grew <= 0)
        if not ok:
            violations.append(
                f"回执 {rec['promo_id']} 晋升 {rec['promoted_count']} 点但正式轴点数未增"
            )
        receipt_growth.append({
            "promo_id": rec["promo_id"],
            "promoted_count": rec["promoted_count"],
            "formal_delta": grew,
            "ok": ok,
        })

    # 2) 回执晋升点必须当前在正式有效轴可见。
    invisible = []
    for rec in receipts:
        for hid in rec["promoted_ids"]:
            if hid not in formal_ids_now:
                invisible.append({"promo_id": rec["promo_id"], "hop_id": hid})
    if invisible:
        violations.append(f"{len(invisible)} 个回执晋升点在正式有效轴不可查")

    # 3) 回执链：ack_seq 严格递增，末条与持久游标一致。
    chain_ok = True
    prev = None
    for rec in receipts:
        if prev is not None and rec["ack_seq"] <= prev:
            chain_ok = False
        if rec["previous_ack_seq"] >= rec["ack_seq"]:
            chain_ok = False
        prev = rec["ack_seq"]
    if receipts and receipts[-1]["ack_seq"] != cur["ack_seq"]:
        chain_ok = False
    if not chain_ok:
        violations.append("晋升回执链与持久 ACK 游标不一致")

    # 4) 点归属完整：primary 未撤回且有序号点不得「两不可查」。
    unaccounted_ids = [
        r["id"]
        for r in conn.execute(
            """
            SELECT h.id FROM hops h
            WHERE h.waybill_id=? AND h.channel='primary' AND h.retracted=0
              AND h.seq IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM formal_hops f WHERE f.id=h.id)
              AND NOT EXISTS (SELECT 1 FROM pending_hops p WHERE p.id=h.id)
            ORDER BY h.id
            """,
            (waybill_id,),
        ).fetchall()
    ]
    if unaccounted_ids:
        violations.append(f"{len(unaccounted_ids)} 个点既不在正式轴也不在待确认区")

    # 5) 待确认不得被静默清掉：末条回执记录的待确认键，若 ACK 未再推进，
    #    只允许因新装载增加，不允许凭空减少。
    silent_cleared = []
    if receipts and receipts[-1]["ack_seq"] == cur["ack_seq"]:
        missing = set(receipts[-1]["pending_after"]) - set(pending_keys)
        silent_cleared = sorted(missing)
        if missing:
            violations.append(f"{len(missing)} 个待确认点无晋升回执却已消失")

    # 6) 投影必须由正式轴末点推导；若正式轴自末条回执以来未变，
    #    投影必须与回执记录的投影一致（禁止「待确认已空但投影停在晋升前」）。
    derived = _derived_projection(conn, waybill_id)
    expected_proj = derived if derived is not None else "未知"
    projection_ok = wb["status_proj"] == expected_proj
    if not projection_ok:
        violations.append(
            f"投影 {wb['status_proj']} 与正式轴末点推导值 {expected_proj} 不一致"
        )
    axis_unchanged_since_receipt = bool(
        receipts
        and receipts[-1]["formal_count_after"] == formal_count
        and receipts[-1]["formal_max_after"] == formal_max
    )
    stale_projection = bool(
        axis_unchanged_since_receipt
        and receipts[-1]["projection_after"] != wb["status_proj"]
    )
    if stale_projection:
        violations.append("正式轴与晋升回执一致，但投影仍停在晋升前")

    checks = {
        "receipt_formal_growth": receipt_growth,
        "receipt_axis_invisible": invisible,
        "receipt_chain_ok": chain_ok,
        "unaccounted_point_ids": unaccounted_ids,
        "silent_cleared_pending_ids": silent_cleared,
        "projection_expected": expected_proj,
        "projection_actual": wb["status_proj"],
        "projection_ok": projection_ok,
        "projection_stale_vs_receipt": stale_projection,
    }

    return {
        "waybill_id": waybill_id,
        "waybill_title": wb["title"],
        "ack_seq": cur["ack_seq"],
        "formal_max_seq": formal_max,
        "formal_count": formal_count,
        "pending_keys": pending_keys,
        "projection": wb["status_proj"],
        "promotion_receipts": receipts,
        "checks": checks,
        "tear": bool(violations),
        "violations": violations,
        "verdict": (
            "撕裂：" + "；".join(violations)
            if violations
            else "无撕裂：晋升回执与正式轴 / 待确认 / 投影一致"
        ),
    }


def list_promotion_tear_audits(conn) -> list[dict]:
    rows = conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
    return [build_promotion_tear_audit(conn, r["id"]) for r in rows]
