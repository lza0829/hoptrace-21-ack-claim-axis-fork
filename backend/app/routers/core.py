from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.modules.ack_cursor_tear import proof as ack_proof
from app.modules.ack_cursor_tear import service as ack
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc
from app.services.ack_demo import run_ordering_demo

router = APIRouter(prefix="/api")


class AckAdvanceBody(BaseModel):
    ack_seq: int = Field(..., ge=0, description="目标 ACK 业务序号；低于当前值即整笔拒绝")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM formal_hops"
            ).fetchone()["c"],
            "hops_pending_ack": conn.execute(
                "SELECT COUNT(*) AS c FROM pending_hops"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT w.id, w.title, w.status_proj, w.created_at,
                   c.ack_seq, c.formal_max_seq,
                   (SELECT COUNT(*) FROM pending_hops p WHERE p.waybill_id=w.id) AS pending_count
            FROM waybills w
            LEFT JOIN ack_cursors c ON c.waybill_id=w.id
            ORDER BY w.id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        formal = timeline_svc.list_primary(conn, waybill_id)
        all_points = timeline_svc.list_all_primary(conn, waybill_id)
        pending = ack.list_pending(conn, waybill_id)
        cursor = ack.get_cursor(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        fork_proof = ack_proof.build_fork_proof(conn, waybill_id)
        return {
            "waybill": dict(wb),
            "hops": formal,
            "all_points": all_points,
            "pending_hops": pending,
            "ack_cursor": cursor,
            "fork_proof": fork_proof,
            "proof": dict(proof) if proof else None,
        }
    finally:
        conn.close()


@router.post("/waybills/{waybill_id}/ack")
def advance_ack(waybill_id: str, body: AckAdvanceBody):
    """ACK 推进入口：上调晋升待确认点；下调整笔拒绝（409，不改史）。"""
    conn = connect()
    try:
        wb = conn.execute("SELECT 1 FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = ack.advance_ack(conn, waybill_id, body.ack_seq)
            conn.commit()
        except ack.DowngradeRejected as e:
            conn.rollback()
            raise HTTPException(409, str(e))
        except ack.AckTearError as e:
            conn.rollback()
            raise HTTPException(500, f"ACK 推进触发失败闭回滚：{e}")
        result["fork_proof"] = ack_proof.build_fork_proof(conn, waybill_id)
        return result
    finally:
        conn.close()


@router.get("/ack-fork-proofs")
def ack_fork_proofs(waybill_id: str | None = None):
    """ACK 分叉证明只读接口：运单、ack_seq、正式最大序号、待确认键集合、结论。"""
    conn = connect()
    try:
        if waybill_id:
            wb = conn.execute("SELECT 1 FROM waybills WHERE id=?", (waybill_id,)).fetchone()
            if not wb:
                raise HTTPException(404, "运单不存在")
            return ack_proof.build_fork_proof(conn, waybill_id)
        return ack_proof.list_fork_proofs(conn)
    finally:
        conn.close()


@router.get("/pending-hops")
def pending_hops(waybill_id: str | None = None):
    """待 ACK 区查询：超 ACK 点可查，不与正式有效轴混淆。"""
    conn = connect()
    try:
        return ack.list_pending(conn, waybill_id)
    finally:
        conn.close()


@router.get("/ack-demo/ordering")
def ack_ordering_demo():
    """先装载后 ACK vs 先 ACK 后装载 对照（独立临时库，只读演示）。"""
    return run_ordering_demo()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
