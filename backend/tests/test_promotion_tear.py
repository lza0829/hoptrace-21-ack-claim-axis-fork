"""晋升宣告与正式轴接续：成功 / 失败对照（夹具 promotion_tear.json）。

硬约束回归：
  1. 禁止回执已晋升而正式轴点数未增；
  2. 禁止待确认已空但投影仍停在晋升前；
  3. 禁止晋升失败却清掉待确认。
"""

import json

import pytest

from app.db import FIXTURES_DIR, connect
from app.modules.ack_cursor_tear import proof as ack_proof
from app.modules.ack_cursor_tear import tear_audit as ack_audit
from app.modules.ack_cursor_tear import service as ack
from app.seed import init_db


@pytest.fixture(autouse=True)
def _db():
    from app.db import DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    yield


def _fixture() -> dict:
    return json.loads((FIXTURES_DIR / "promotion_tear.json").read_text(encoding="utf-8"))


def _load_over_cursor_points(conn, fixture: dict, wid: str) -> None:
    """先装超游标点：ACK=0 时全部只能落待确认区。"""
    conn.execute(
        "INSERT INTO waybills(id,title,status_proj,created_at) VALUES (?,?,?,?)",
        (wid, fixture["waybill"]["title"], "未知", fixture["waybill"]["created_at"]),
    )
    ack.ensure_cursor(conn, wid)
    for hop in fixture["hops"]:
        row = dict(hop, waybill_id=wid)
        conn.execute(
            "INSERT INTO hops(id,waybill_id,event_ts,location_code,event_code,seq,channel) "
            "VALUES (?,?,?,?,?,?,'primary')",
            (row["id"], wid, row["event_ts"], row["location_code"], row["event_code"], row["seq"]),
        )
        assert ack.gate_hop(conn, row) == "pending"
    ack.recompute_formal_max(conn, wid)
    ack.refresh_status_projection(conn, wid)
    ack.assert_no_tear(conn, wid)


def test_success_promotion_makes_points_queryable_and_advances_projection():
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        r = ack.advance_ack(conn, wid, fx["advance_to"])
        conn.commit()

        covered = [h["id"] for h in fx["hops"] if h["seq"] <= fx["advance_to"]]
        assert sorted(r["promoted"]) == sorted(covered)
        assert r["promotion_proof_id"]

        # 晋升点确实进入正式有效轴（可查），待确认只剩未覆盖点。
        formal_ids = [x["id"] for x in conn.execute(
            "SELECT id FROM formal_hops WHERE waybill_id=? ORDER BY seq", (wid,))]
        assert formal_ids == covered
        assert [p["id"] for p in ack.list_pending(conn, wid)] == [
            h["id"] for h in fx["hops"] if h["seq"] > fx["advance_to"]
        ]
        # 投影同事务前进，且由正式轴末点推导。
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id=?", (wid,)).fetchone()
        assert wb["status_proj"] == "到站"
        # 晋升回执落库（独立晋升对照表），与正式轴一致。
        receipts = ack_audit.list_promotion_receipts(conn, wid)
        assert len(receipts) == 1
        assert receipts[0]["promoted_ids"] == covered
        assert receipts[0]["projection_before"] == "未知"
        assert receipts[0]["projection_after"] == "到站"
        assert receipts[0]["formal_count_before"] == 0
        assert receipts[0]["formal_count_after"] == len(covered)
        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        assert audit["tear"] is False, audit["violations"]
    finally:
        conn.close()


def test_failure_keeps_pending_and_projection_at_pre_promotion_state():
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        conn.commit()
        before_pending = [p["id"] for p in ack.list_pending(conn, wid)]
        before_proj = conn.execute(
            "SELECT status_proj FROM waybills WHERE id=?", (wid,)).fetchone()["status_proj"]
        before_formal = conn.execute(
            "SELECT COUNT(*) c FROM formal_hops WHERE waybill_id=?", (wid,)).fetchone()["c"]
    finally:
        conn.close()

    real_refresh = ack.refresh_status_projection

    def boom(c, w):
        raise ack.AckTearError("模拟投影重算写入失败")

    ack.refresh_status_projection = boom
    try:
        conn = connect()
        try:
            with pytest.raises(ack.AckTearError):
                conn.execute("BEGIN IMMEDIATE")
                ack.advance_ack(conn, wid, fx["advance_to"])
        finally:
            conn.rollback()
            conn.close()
    finally:
        ack.refresh_status_projection = real_refresh

    conn = connect()
    try:
        # 晋升失败：待确认集合与投影保持晋升前，正式轴未增，不回执。
        assert [p["id"] for p in ack.list_pending(conn, wid)] == before_pending
        assert conn.execute(
            "SELECT status_proj FROM waybills WHERE id=?", (wid,)).fetchone()["status_proj"] == before_proj
        assert conn.execute(
            "SELECT COUNT(*) c FROM formal_hops WHERE waybill_id=?", (wid,)).fetchone()["c"] == before_formal
        assert ack.get_cursor(conn, wid)["ack_seq"] == 0
        assert ack_audit.list_promotion_receipts(conn, wid) == []
        assert ack_audit.build_promotion_tear_audit(conn, wid)["tear"] is False
    finally:
        conn.close()


def test_receipt_promoted_point_invisible_on_formal_axis_rolls_back():
    """回执已列晋升点、正式轴却不可见：必须回滚，不得提交‘宣告了但没接上’。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        conn.commit()
    finally:
        conn.close()

    real_promote = ack.promote_pending

    def torn_promote(c, w):
        ids = real_promote(c, w)
        if ids:
            # 模拟晋升点基表行缺失，正式轴视图随即不可查。
            c.execute("DELETE FROM hops WHERE id=?", (ids[0],))
        return ids

    ack.promote_pending = torn_promote
    try:
        conn = connect()
        try:
            with pytest.raises(ack.AckTearError, match="晋升撕裂"):
                conn.execute("BEGIN IMMEDIATE")
                ack.advance_ack(conn, wid, fx["advance_to"])
        finally:
            conn.rollback()
            conn.close()
    finally:
        ack.promote_pending = real_promote

    conn = connect()
    try:
        assert len(ack.list_pending(conn, wid)) == len(fx["hops"])
        assert ack_audit.list_promotion_receipts(conn, wid) == []
        assert conn.execute(
            "SELECT COUNT(*) c FROM formal_hops WHERE waybill_id=?", (wid,)).fetchone()["c"] == 0
    finally:
        conn.close()


def test_audit_detects_receipt_without_formal_growth():
    """核证接口必须能识别‘回执已晋升而正式轴点数未增’的撕裂（人为制造损坏）。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        ack.advance_ack(conn, wid, fx["advance_to"])
        conn.commit()

        # 人为把回执改成‘晋升了但正式轴未增’。
        conn.execute(
            "UPDATE promotion_proofs SET formal_count_after=formal_count_before WHERE waybill_id=?",
            (wid,),
        )
        conn.commit()
        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        assert audit["tear"] is True
        assert any("正式轴点数未增" in v for v in audit["violations"])
    finally:
        conn.close()


def test_audit_detects_silent_pending_clear_without_receipt():
    """绕过服务入口直改游标并清待确认：正式轴可见但投影停在晋升前，必须判撕裂。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        conn.commit()
        # 不经 advance_ack：直接推进游标、删待确认，不重算投影、不写回执。
        conn.execute("UPDATE ack_cursors SET ack_seq=? WHERE waybill_id=?", (fx["advance_to"], wid))
        conn.execute("DELETE FROM pending_hops WHERE waybill_id=?", (wid,))
        conn.commit()
        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        assert audit["tear"] is True
        assert audit["checks"]["projection_ok"] is False
        assert any("投影" in v for v in audit["violations"])
    finally:
        conn.close()


def test_audit_detects_unaccounted_points():
    """点既不在正式轴也不在待确认区（两不可查）必须判撕裂。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        conn.commit()
        conn.execute("DELETE FROM pending_hops WHERE waybill_id=?", (wid,))
        conn.commit()
        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        assert audit["tear"] is True
        assert audit["checks"]["unaccounted_point_ids"] == [h["id"] for h in fx["hops"]]
    finally:
        conn.close()


def test_audit_detects_projection_left_at_pre_promotion_value():
    """正式轴 / 待确认 / 回执都对，但投影被人为改回晋升前：核证必须判撕裂。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        ack.advance_ack(conn, wid, fx["advance_to"])
        conn.commit()

        # 晋升已完整落库，只把运单投影改回晋升前的「未知」。
        conn.execute("UPDATE waybills SET status_proj='未知' WHERE id=?", (wid,))
        conn.commit()

        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        assert audit["tear"] is True
        assert audit["checks"]["projection_ok"] is False
        assert audit["checks"]["projection_stale_vs_receipt"] is True
        assert any("投影" in v for v in audit["violations"])
    finally:
        conn.close()


def test_advance_beyond_all_points_is_not_noop_and_writes_receipt():
    """ACK 推进到覆盖全部点之后：无点可晋升但游标前进，仍需落回执且不判撕裂。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        ack.advance_ack(conn, wid, fx["advance_to"])
        ack.advance_ack(conn, wid, 9)  # 覆盖剩余 P-4
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        r = ack.advance_ack(conn, wid, 12)  # 再无点可晋升，仅游标前进
        conn.commit()
        assert r["noop"] is False
        assert r["promoted"] == []
        assert r["promotion_proof_id"]
        assert ack.list_pending(conn, wid) == []
        receipts = ack_audit.list_promotion_receipts(conn, wid)
        assert receipts[-1]["ack_seq"] == 12 and receipts[-1]["promoted_ids"] == []
        assert ack_audit.build_promotion_tear_audit(conn, wid)["tear"] is False
    finally:
        conn.close()


def test_assert_no_tear_rejects_projection_stale_after_promotion():
    """提交前不变量：投影与正式轴末点不一致即判分叉。"""
    fx = _fixture()
    wid = fx["waybill"]["id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _load_over_cursor_points(conn, fx, wid)
        ack.advance_ack(conn, wid, fx["advance_to"])
        conn.execute("UPDATE waybills SET status_proj='未知' WHERE id=?", (wid,))
        with pytest.raises(ack.AckTearError, match="投影"):
            ack.assert_no_tear(conn, wid)
    finally:
        conn.rollback()
        conn.close()


def test_promotion_demo_contrast_uses_fixture():
    from app.services.promotion_demo import run_promotion_tear_demo

    result = run_promotion_tear_demo()
    assert result["fixture"] == "promotion_tear.json"
    ok = result["success"]
    assert ok["load"]["load_verdicts"][0]["verdict"] == "pending"
    assert ok["before"]["pending_ids"] == [h["id"] for h in _fixture()["hops"]]
    assert ok["after"]["formal_ids"] == ["P-1", "P-2", "P-3"]
    assert ok["after"]["pending_ids"] == ["P-4"]
    assert ok["after"]["projection"] == "到站"
    assert ok["after"]["promotion_receipts"] == 1
    assert ok["audit"]["tear"] is False

    modes = {f["name"]: f for f in result["failures"]}
    assert set(modes) == {"失败：projection_write_fails", "失败：formal_axis_invisible"}
    for f in result["failures"]:
        assert f["rolled_back"] is True
        assert f["error"]
        assert f["after"]["pending_ids"] == f["before"]["pending_ids"]
        assert f["after"]["projection"] == f["before"]["projection"]


def test_promotion_endpoints(client):
    r = client.post("/api/waybills/WB-1001/ack", json={"ack_seq": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["tear_audit"]["tear"] is False
    assert body["tear_audit"]["checks"]["projection_ok"] is True
    assert body["promotion_proof_id"]

    receipts = client.get("/api/promotion-proofs?waybill_id=WB-1001").json()
    assert len(receipts) == 1
    assert receipts[0]["promoted_ids"] == ["H-1", "H-2"]
    assert receipts[0]["projection_before"] == "未知"
    assert receipts[0]["projection_after"] == "运输中"

    audit = client.get("/api/promotion-tear-audits?waybill_id=WB-1001").json()
    assert audit["tear"] is False
    assert set(audit["checks"]) >= {
        "receipt_formal_growth", "projection_ok", "unaccounted_point_ids",
    }
    listed = client.get("/api/promotion-tear-audits").json()
    assert {a["waybill_id"] for a in listed} >= {"WB-1001", "WB-1002"}

    assert client.get("/api/promotion-tear-audits?waybill_id=NOPE").status_code == 404
    assert client.get("/api/promotion-proofs?waybill_id=NOPE").status_code == 404


def test_promotion_demo_endpoint(client):
    data = client.get("/api/promotion-demo/tear").json()
    assert data["success"]["after"]["formal_ids"] == ["P-1", "P-2", "P-3"]
    assert all(f["rolled_back"] for f in data["failures"])
