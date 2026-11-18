import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.domain import EVENT_STATUS
from app.modules.ack_cursor_tear import schema as ack_schema
from app.modules.ack_cursor_tear import service as ack_service


SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    ack_schema.init_ack_schema(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], wb.get("status_proj", "未知"), wb["created_at"]),
        )
        ack_service.ensure_cursor(conn, wb["id"])
    # 种子装载同样过 ACK 分流闸：初始 ACK=0，种子点全部进待确认区，
    # 由设置 / 运单页推进 ACK 后晋升，便于演示装载闸与分叉证明。
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
        ack_service.gate_hop(conn, hop)
    _refresh_projections(conn)
    for wb in data.get("waybills", []):
        ack_service.recompute_formal_max(conn, wb["id"])
        ack_service.assert_no_tear(conn, wb["id"])


def _refresh_projections(conn):
    """投影只由正式有效轴 formal_hops 推导；待确认点不参与。"""
    rows = conn.execute(
        """
        SELECT f.waybill_id, f.id, f.event_code
        FROM formal_hops f
        JOIN (
          SELECT waybill_id, MAX(event_ts || '|' || id) AS mk
          FROM formal_hops GROUP BY waybill_id
        ) t ON t.waybill_id=f.waybill_id AND (f.event_ts || '|' || f.id)=t.mk
        """
    ).fetchall()
    seen = set()
    for r in rows:
        proj = EVENT_STATUS.get(r["event_code"], "未知")
        conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, r["waybill_id"]))
        conn.execute(
            "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,datetime('now'))",
            (r["waybill_id"], r["id"], r["event_code"], proj),
        )
        seen.add(r["waybill_id"])
    # 正式轴为空的运单：投影归「未知」，留空证明记录，避免旧投影伪装正式结果。
    empty_rows = conn.execute(
        "SELECT id FROM waybills WHERE id NOT IN (SELECT DISTINCT waybill_id FROM formal_hops)"
    ).fetchall()
    for r in empty_rows:
        conn.execute("UPDATE waybills SET status_proj='未知' WHERE id=?", (r["id"],))
        conn.execute(
            "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,datetime('now'))",
            (r["id"], None, None, "未知"),
        )
