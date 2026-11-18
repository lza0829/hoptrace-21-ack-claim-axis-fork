"""服务层硬约束测试：分流闸、晋升、拒降、失败闭不变量。"""

import pytest

from app.db import connect
from app.modules.ack_cursor_tear import service as ack
from app.seed import init_db


@pytest.fixture(autouse=True)
def _db():
    from app.db import DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    yield


def _hop(hid, wid, seq, code="IN_TRANSIT", ts="2026-11-18T08:00:00Z"):
    return {"id": hid, "waybill_id": wid, "event_ts": ts,
            "location_code": "LOC", "event_code": code, "seq": seq}


def test_seed_lands_in_pending_when_ack_zero():
    conn = connect()
    try:
        cur = ack.get_cursor(conn, "WB-1001")
        assert cur["ack_seq"] == 0
        assert cur["formal_max_seq"] == 0
        pending = ack.list_pending(conn, "WB-1001")
        assert {p["id"] for p in pending} == {"H-1", "H-2", "H-3"}
        formal = conn.execute("SELECT COUNT(*) c FROM formal_hops WHERE waybill_id='WB-1001'").fetchone()["c"]
        assert formal == 0
    finally:
        conn.close()


def test_gate_splits_by_persisted_ack():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ack.advance_ack(conn, "WB-1002", 1)  # H-4(seq1) 晋升
        conn.commit()
        cur = ack.get_cursor(conn, "WB-1002")
        assert cur["ack_seq"] == 1
        assert cur["formal_max_seq"] == 1
        assert ack.list_pending(conn, "WB-1002") == []
        formal = conn.execute("SELECT id FROM formal_hops WHERE waybill_id='WB-1002'").fetchall()
        assert [r["id"] for r in formal] == ["H-4"]
    finally:
        conn.close()


def test_advance_promotes_covered_pending_and_recomputes_projection():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = ack.advance_ack(conn, "WB-1001", 3)
        conn.commit()
        assert sorted(result["promoted"]) == ["H-1", "H-2", "H-3"]
        cur = ack.get_cursor(conn, "WB-1001")
        assert cur["ack_seq"] == 3 and cur["formal_max_seq"] == 3
        assert ack.list_pending(conn, "WB-1001") == []
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id='WB-1001'").fetchone()
        assert wb["status_proj"] == "到站"  # 仅由正式轴末点 H-3 推导
    finally:
        conn.close()


def test_downgrade_rejected_and_history_untouched():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ack.advance_ack(conn, "WB-1001", 3)
        conn.commit()
        before_pending = len(ack.list_pending(conn, "WB-1001"))
        before_max = ack.get_cursor(conn, "WB-1001")["formal_max_seq"]
        with pytest.raises(ack.DowngradeRejected):
            conn.execute("BEGIN IMMEDIATE")
            ack.advance_ack(conn, "WB-1001", 2)
        conn.rollback()
        cur = ack.get_cursor(conn, "WB-1001")
        assert cur["ack_seq"] == 3
        assert cur["formal_max_seq"] == before_max
        assert len(ack.list_pending(conn, "WB-1001")) == before_pending
    finally:
        conn.close()


def test_load_gate_holds_over_ack_points_out_of_formal_axis():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 新运单 ACK=0 装载三个点：全部待确认
        conn.execute(
            "INSERT INTO waybills(id,title,status_proj,created_at) VALUES ('WB-T','T','未知','2026-11-18T00:00:00Z')"
        )
        ack.ensure_cursor(conn, "WB-T")
        for i, code in enumerate(["PICKED", "IN_TRANSIT", "ARRIVED_HUB"], start=1):
            h = _hop(f"T-{i}", "WB-T", i, code)
            conn.execute(
                "INSERT INTO hops(id,waybill_id,event_ts,location_code,event_code,seq,channel) "
                "VALUES (?,?,?,?,?,?,'primary')",
                (h["id"], h["waybill_id"], h["event_ts"], h["location_code"], h["event_code"], h["seq"]),
            )
            verdict = ack.gate_hop(conn, h)
            assert verdict == "pending"
        ack.assert_no_tear(conn, "WB-T")
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        ack.advance_ack(conn, "WB-T", 2)  # 前两点晋升，第三点仍待确认
        conn.commit()
        formal_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM formal_hops WHERE waybill_id='WB-T' ORDER BY seq")]
        assert formal_ids == ["T-1", "T-2"]
        assert [p["id"] for p in ack.list_pending(conn, "WB-T")] == ["T-3"]
        proof_row = conn.execute(
            "SELECT status_proj FROM waybills WHERE id='WB-T'").fetchone()
        assert proof_row["status_proj"] == "运输中"  # 待确认的 T-3 不参与投影
    finally:
        conn.close()


def test_assert_no_tear_detects_over_ack_formal_point():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ack.advance_ack(conn, "WB-1002", 1)
        conn.commit()
        # 人为制造分叉：把 ACK 调回 0（绕过服务入口直改库），正式点 H-4 变成超 ACK 点
        conn.execute("UPDATE ack_cursors SET ack_seq=0 WHERE waybill_id='WB-1002'")
        with pytest.raises(ack.AckTearError):
            ack.assert_no_tear(conn, "WB-1002")
    finally:
        conn.rollback()
        conn.close()


def test_missing_seq_point_is_held_pending():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO waybills(id,title,status_proj,created_at) VALUES ('WB-N','N','未知','2026-11-18T00:00:00Z')"
        )
        ack.ensure_cursor(conn, "WB-N")
        h = _hop("N-1", "WB-N", None)
        conn.execute(
            "INSERT INTO hops(id,waybill_id,event_ts,location_code,event_code,seq,channel) "
            "VALUES (?,?,?,?,?,?,'primary')",
            (h["id"], h["waybill_id"], h["event_ts"], h["location_code"], h["event_code"], h["seq"]),
        )
        verdict = ack.gate_hop(conn, h)
        assert verdict == "pending"
        held = ack.list_pending(conn, "WB-N")[0]
        assert held["held_reason"] == "missing_seq"
    finally:
        conn.rollback()
        conn.close()
