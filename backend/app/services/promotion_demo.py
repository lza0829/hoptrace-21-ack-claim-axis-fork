"""晋升撕裂对照演示：同一夹具、同一 ACK 上调，成功与失败两条路径对照。

夹具 fixtures/promotion_tear.json 先装载全部「超游标点」（ACK=0，全部落待确认区），
再上调 ACK 覆盖其中若干点：
  - 成功路径：晋升点进入正式有效轴、投影随同事务前进、写晋升回执；
  - 失败路径：在晋升事务中途注入撕裂（投影重算失败 / 正式轴不可见），
    整趟回滚 —— 待确认集合与投影保持晋升前，且不产生晋升回执。

每条路径都在独立临时 SQLite 库运行，不污染主库。
"""

import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

from app.db import DATA_DIR, FIXTURES_DIR
from app.modules.ack_cursor_tear import tear_audit as ack_audit
from app.modules.ack_cursor_tear import schema as ack_schema
from app.modules.ack_cursor_tear import service as ack
from app.seed import SCHEMA as BASE_SCHEMA

FIXTURE_NAME = "promotion_tear.json"


def _load_fixture() -> dict:
    path = FIXTURES_DIR / FIXTURE_NAME
    if not path.exists():
        raise FileNotFoundError(f"缺少夹具 {FIXTURE_NAME}")
    return json.loads(path.read_text(encoding="utf-8"))


def _new_conn() -> tuple[sqlite3.Connection, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="promo_demo_", suffix=".db", dir=DATA_DIR)
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
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")


def _seed_and_load(conn, fixture: dict) -> tuple[str, dict]:
    """先建运单、再装载全部超游标点：ACK=0 时它们只能落待确认区。"""
    wid = fixture["waybill"]["id"]
    conn.execute(
        "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
        (wid, fixture["waybill"]["title"], "未知", fixture["waybill"]["created_at"]),
    )
    ack.ensure_cursor(conn, wid)
    ack.advance_ack(conn, wid, fixture.get("initial_ack_seq", 0))

    verdicts = []
    for hop in fixture["hops"]:
        row = dict(hop, waybill_id=wid)
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
            "VALUES (?,?,?,?,?,?,'primary')",
            (row["id"], wid, row["event_ts"], row["location_code"], row["event_code"], row["seq"]),
        )
        verdicts.append({"hop_id": row["id"], "seq": row["seq"],
                         "verdict": ack.gate_hop(conn, row)})
    ack.recompute_formal_max(conn, wid)
    ack.refresh_status_projection(conn, wid)
    ack.assert_no_tear(conn, wid)
    return wid, {"load_verdicts": verdicts}


class _patch:
    """临时替换 service 模块上的函数，退出时恢复（演示用，不依赖测试框架）。"""

    def __init__(self, name: str, replacement):
        self.name = name
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = getattr(ack, self.name)
        setattr(ack, self.name, self.replacement)
        return self

    def __exit__(self, *exc):
        setattr(ack, self.name, self.original)
        return False


def _state(conn, wid: str) -> dict:
    cur = ack.get_cursor(conn, wid)
    wb = conn.execute("SELECT status_proj FROM waybills WHERE id=?", (wid,)).fetchone()
    return {
        "ack_seq": cur["ack_seq"],
        "formal_max_seq": cur["formal_max_seq"],
        "formal_ids": [
            r["id"] for r in conn.execute(
                "SELECT id FROM formal_hops WHERE waybill_id=? ORDER BY seq", (wid,)
            ).fetchall()
        ],
        "pending_ids": [p["id"] for p in ack.list_pending(conn, wid)],
        "projection": wb["status_proj"],
        "promotion_receipts": len(ack_audit.list_promotion_receipts(conn, wid)),
    }


def _run_success(fixture: dict) -> dict:
    conn, path = _new_conn()
    try:
        _begin(conn)
        wid, load = _seed_and_load(conn, fixture)
        conn.commit()
        before = _state(conn, wid)

        _begin(conn)
        receipt = ack.advance_ack(conn, wid, fixture["advance_to"])
        conn.commit()
        after = _state(conn, wid)
        audit = ack_audit.build_promotion_tear_audit(conn, wid)
        return {
            "name": "成功：先装超游标点再上调 ACK",
            "waybill_id": wid,
            "load": load,
            "before": before,
            "receipt": receipt,
            "after": after,
            "audit": audit,
            "committed": True,
        }
    finally:
        _close(conn, path)


def _run_failure(fixture: dict, mode: str) -> dict:
    conn, path = _new_conn()
    try:
        _begin(conn)
        wid, load = _seed_and_load(conn, fixture)
        conn.commit()
        before = _state(conn, wid)

        error = None
        try:
            _begin(conn)
            if mode == "projection_write_fails":
                def boom(c, w):
                    raise ack.AckTearError("模拟投影重算写入失败")
                with _patch("refresh_status_projection", boom):
                    ack.advance_ack(conn, wid, fixture["advance_to"])
            elif mode == "formal_axis_invisible":
                real_promote = ack.promote_pending

                def torn_promote(c, w):
                    ids = real_promote(c, w)
                    # 模拟晋升点基表行丢失：已离开待确认区却在正式轴不可查。
                    if ids:
                        c.execute("DELETE FROM hops WHERE id=?", (ids[0],))
                    return ids

                with _patch("promote_pending", torn_promote):
                    ack.advance_ack(conn, wid, fixture["advance_to"])
            else:
                raise ValueError(f"未知失败模式 {mode}")
            conn.commit()
        except ack.AckTearError as e:
            conn.rollback()
            error = str(e)

        after = _state(conn, wid)
        return {
            "name": f"失败：{mode}",
            "waybill_id": wid,
            "load": load,
            "before": before,
            "error": error,
            "after": after,
            "rolled_back": (
                error is not None
                and after["pending_ids"] == before["pending_ids"]
                and after["projection"] == before["projection"]
                and after["formal_ids"] == before["formal_ids"]
                and after["promotion_receipts"] == before["promotion_receipts"]
            ),
        }
    finally:
        _close(conn, path)


def run_promotion_tear_demo() -> dict:
    """返回成功 / 失败对照；每次调用都换运单号，避免同名库残留混淆。"""
    fixture = _load_fixture()
    fixture = json.loads(json.dumps(fixture))
    fixture["waybill"]["id"] = f"{fixture['waybill']['id']}-{uuid.uuid4().hex[:6]}"

    success = _run_success(fixture)
    failures = [
        _run_failure(fixture, "projection_write_fails"),
        _run_failure(fixture, "formal_axis_invisible"),
    ]
    return {
        "fixture": FIXTURE_NAME,
        "advance_to": fixture["advance_to"],
        "success": success,
        "failures": failures,
        "verdict": {
            "success": (
                "晋升后待确认集合缩小、正式轴点数增加且晋升点可查、投影随同事务前进，"
                "晋升回执与正式轴一致（无撕裂）。"
            ),
            "failure": (
                "晋升事务中途失败整趟回滚：待确认集合、正式轴与投影保持晋升前，不写晋升回执。"
            ),
        },
    }
