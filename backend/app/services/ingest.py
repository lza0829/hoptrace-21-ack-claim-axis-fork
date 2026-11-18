import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules.ack_cursor_tear import service as ack


def run_batch() -> dict:
    """装载批次：ACK 分流闸（装载时按 ACK 分流正式 / 待确认）。

    单事务失败闭 —— 任一点写入或不变量校验失败，整趟回滚，
    不留「投影已变而 ACK / 待确认证明未写」的中间态。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        formal = 0
        pending = 0
        duplicate = 0
        touched: set[str] = set()
        for hop in hops:
            wid = hop["waybill_id"]
            exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            ack.ensure_cursor(conn, wid)
            conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                duplicate += 1
                continue
            # 装载闸：依据持久 ACK 判定正式 / 待确认，分流结果随本事务落库。
            verdict = ack.gate_hop(conn, hop)
            if verdict == "formal":
                formal += 1
            else:
                pending += 1
            touched.add(wid)

        for wid in touched:
            ack.recompute_formal_max(conn, wid)
            ack.refresh_status_projection(conn, wid)
            # 提交前硬校验：正式轴不得含超 ACK 点，失败即抛错整趟回滚。
            ack.assert_no_tear(conn, wid)

        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": formal + pending,
            "formal": formal,
            "pending_ack": pending,
            "duplicate": duplicate,
            "input": len(hops),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
