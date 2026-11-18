"""失败闭：装载 / ACK 事务中途失败必须整趟回滚，不留半写状态。"""

import pytest

from app.db import DB_PATH, connect
from app.modules.ack_cursor_tear import service as ack
from app.seed import init_db


@pytest.fixture(autouse=True)
def _db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    yield


def test_ingest_batch_rolls_back_entirely_when_gate_fails(monkeypatch):
    from app.services import ingest as ingest_svc

    conn = connect()
    try:
        before_hops = conn.execute("SELECT COUNT(*) c FROM hops").fetchone()["c"]
        before_pending = conn.execute("SELECT COUNT(*) c FROM pending_hops").fetchone()["c"]
    finally:
        conn.close()

    call = {"n": 0}
    real_gate = ack.gate_hop

    def flaky_gate(c, hop):
        call["n"] += 1
        if call["n"] == 2:
            raise ack.AckTearError("模拟第二点剔除失败")
        return real_gate(c, hop)

    monkeypatch.setattr(ingest_svc.ack, "gate_hop", flaky_gate)
    report = ingest_svc.run_batch()
    assert report["ok"] is False
    assert "模拟第二点剔除失败" in report["error"]

    conn = connect()
    try:
        after_hops = conn.execute("SELECT COUNT(*) c FROM hops").fetchone()["c"]
        after_pending = conn.execute("SELECT COUNT(*) c FROM pending_hops").fetchone()["c"]
        # 整趟回滚：hops / pending / 批次记录均无部分写入
        assert after_hops == before_hops
        assert after_pending == before_pending
        batch = conn.execute("SELECT COUNT(*) c FROM ingest_batches WHERE id='B-DEMO-01'").fetchone()["c"]
        assert batch == 0
        # 失败闭校验仍可通过（库里没有被破坏的状态）
        for wid in ("WB-1001", "WB-1002"):
            ack.assert_no_tear(conn, wid)
    finally:
        conn.close()


def test_advance_ack_rolls_back_when_invariant_breaks(monkeypatch):
    conn = connect()
    try:
        # 让晋升后的不变量校验必然失败，确认 ACK 写入也被回滚
        def boom(c, wid):
            raise ack.AckTearError("模拟提交前校验失败")
        monkeypatch.setattr(ack, "assert_no_tear", boom)
        with pytest.raises(ack.AckTearError):
            conn.execute("BEGIN IMMEDIATE")
            ack.advance_ack(conn, "WB-1001", 3)
        conn.rollback()
    finally:
        conn.close()

    conn = connect()
    try:
        cur = ack.get_cursor(conn, "WB-1001")
        assert cur["ack_seq"] == 0
        # 待确认点一个都没被晋升
        pending_ids = {p["id"] for p in ack.list_pending(conn, "WB-1001")}
        assert pending_ids == {"H-1", "H-2", "H-3"}
    finally:
        conn.close()


def test_state_is_durable_on_disk_not_memory_only():
    """禁止只在内存比较游标：ACK / pending 必须落库，重连后仍在。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ack.advance_ack(conn, "WB-1002", 1)
        conn.commit()
    finally:
        conn.close()

    # 全新连接模拟进程重启
    conn2 = connect()
    try:
        cur = ack.get_cursor(conn2, "WB-1002")
        assert cur["ack_seq"] == 1 and cur["formal_max_seq"] == 1
        formal = conn2.execute(
            "SELECT id FROM formal_hops WHERE waybill_id='WB-1002' ORDER BY seq"
        ).fetchall()
        assert [r["id"] for r in formal] == ["H-4"]
        assert ack.list_pending(conn2, "WB-1002") == []
    finally:
        conn2.close()
