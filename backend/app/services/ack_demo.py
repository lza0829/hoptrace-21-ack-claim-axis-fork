"""ACK 推进顺序对照演示：同一批点、同一最终 ACK，仅装载与 ACK 的先后不同。

使用独立临时库，不污染主库；每步留存 ACK 分叉证明快照，凸显正式轴可区分的中间结果。
"""

import os
import sqlite3
import tempfile
from pathlib import Path

from app.db import DATA_DIR
from app.modules.ack_cursor_tear import proof as ack_proof
from app.modules.ack_cursor_tear import schema as ack_schema
from app.modules.ack_cursor_tear import service as ack
from app.seed import SCHEMA as BASE_SCHEMA

DEMO_HOPS = [
    {"id": "D-H1", "event_ts": "2026-11-18T08:00:00Z", "location_code": "SHA-HUB",
     "event_code": "PICKED", "seq": 1},
    {"id": "D-H2", "event_ts": "2026-11-18T10:00:00Z", "location_code": "NJ-HUB",
     "event_code": "IN_TRANSIT", "seq": 2},
    {"id": "D-H3", "event_ts": "2026-11-18T13:00:00Z", "location_code": "HZ-HUB",
     "event_code": "ARRIVED_HUB", "seq": 3},
    {"id": "D-H4", "event_ts": "2026-11-18T16:00:00Z", "location_code": "HZ-STATION",
     "event_code": "OUT_FOR_DELIVERY", "seq": 4},
]


def _new_conn() -> tuple[sqlite3.Connection, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="ack_demo_", suffix=".db", dir=DATA_DIR)
    os.close(fd)
    Path(path).unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(BASE_SCHEMA)
    ack_schema.init_ack_schema(conn)
    return conn, path


def _close(conn: sqlite3.Connection, path: str) -> None:
    conn.close()
    Path(path).unlink(missing_ok=True)


def _begin(conn: sqlite3.Connection) -> None:
    """失败的事务回滚后驱动可能仍记账在事务内，先确保起点干净。"""
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")


def _make_waybill(conn, wid: str) -> None:
    conn.execute(
        "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
        (wid, f"演示-{wid}", "未知", "2026-11-18T07:00:00Z"),
    )
    ack.ensure_cursor(conn, wid)


def _load_points(conn, wid: str, hops: list[dict]) -> dict:
    formal, pending = [], []
    for hop in hops:
        row = dict(hop, waybill_id=wid)
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
            "VALUES (?,?,?,?,?,?,'primary')",
            (row["id"], wid, row["event_ts"], row["location_code"], row["event_code"], row["seq"]),
        )
        verdict = ack.gate_hop(conn, row)
        (formal if verdict == "formal" else pending).append(row["id"])
    ack.recompute_formal_max(conn, wid)
    ack.refresh_status_projection(conn, wid)
    ack.assert_no_tear(conn, wid)
    return {"formal_ids": formal, "pending_ids": pending}


def _snapshot(conn, wid: str) -> dict:
    return ack_proof.build_fork_proof(conn, wid)


def run_ordering_demo() -> dict:
    """对照两条路径，返回逐步 trace；每步带分叉证明，结尾附可区分性结论。"""
    trace: dict = {"hops": [h["id"] for h in DEMO_HOPS], "scenarios": []}

    # 路径甲：先装载后 ACK
    conn, path = _new_conn()
    try:
        wid = "WB-DEMO-LOAD-FIRST"
        _begin(conn)
        _make_waybill(conn, wid)
        conn.commit()
        steps_a = [{"step": "init", "proof": _snapshot(conn, wid)}]
        _begin(conn)
        load = _load_points(conn, wid, DEMO_HOPS)
        conn.commit()
        steps_a.append({"step": "先装载（ACK=0）", "action": "load", "result": load,
                        "proof": _snapshot(conn, wid)})

        _begin(conn)
        r1 = ack.advance_ack(conn, wid, 2)
        conn.commit()
        steps_a.append({"step": "ACK 上调 0→2", "action": "advance_ack", "result": r1,
                        "proof": _snapshot(conn, wid)})

        _begin(conn)
        r2 = ack.advance_ack(conn, wid, 4)
        conn.commit()
        steps_a.append({"step": "ACK 上调 2→4", "action": "advance_ack", "result": r2,
                        "proof": _snapshot(conn, wid)})

        downgrade = {"rejected": False}
        try:
            _begin(conn)
            ack.advance_ack(conn, wid, 3)
            conn.commit()
        except ack.DowngradeRejected as e:
            conn.rollback()
            downgrade = {"rejected": True, "reason": str(e), "proof": _snapshot(conn, wid)}
        steps_a.append({"step": "尝试 ACK 下调 4→3", "action": "downgrade", "result": downgrade,
                        "proof": _snapshot(conn, wid)})
        trace["scenarios"].append({
            "name": "先装载后 ACK",
            "waybill_id": wid,
            "steps": steps_a,
        })
    finally:
        _close(conn, path)

    # 路径乙：先 ACK 后装载
    conn, path = _new_conn()
    try:
        wid = "WB-DEMO-ACK-FIRST"
        _begin(conn)
        _make_waybill(conn, wid)
        conn.commit()
        steps_b = [{"step": "init", "proof": _snapshot(conn, wid)}]
        _begin(conn)
        r0 = ack.advance_ack(conn, wid, 4)
        conn.commit()
        steps_b.append({"step": "先推进 ACK 0→4", "action": "advance_ack", "result": r0,
                        "proof": _snapshot(conn, wid)})

        _begin(conn)
        load = _load_points(conn, wid, DEMO_HOPS)
        conn.commit()
        steps_b.append({"step": "后装载同批点", "action": "load", "result": load,
                        "proof": _snapshot(conn, wid)})
        trace["scenarios"].append({
            "name": "先 ACK 后装载",
            "waybill_id": wid,
            "steps": steps_b,
        })
    finally:
        _close(conn, path)

    a_after_load = trace["scenarios"][0]["steps"][1]["proof"]
    b_after_load = trace["scenarios"][1]["steps"][2]["proof"]
    trace["distinction"] = {
        "load_first_after_load": {
            "formal_max_seq": a_after_load["formal_max_seq"],
            "formal_count": a_after_load["formal_count"],
            "pending_keys": a_after_load["pending_keys"],
        },
        "ack_first_after_load": {
            "formal_max_seq": b_after_load["formal_max_seq"],
            "formal_count": b_after_load["formal_count"],
            "pending_keys": b_after_load["pending_keys"],
        },
        "verdict": (
            "同一批点、同一最终 ACK=4：先装载后 ACK 在装载瞬间正式轴为空、四点全在待确认区；"
            "先 ACK 后装载在装载瞬间四点直接入正式轴。正式轴中间结果可由分叉证明区分。"
        ),
    }
    return trace
