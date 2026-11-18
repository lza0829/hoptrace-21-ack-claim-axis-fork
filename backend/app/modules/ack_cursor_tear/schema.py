"""ACK 游标 / 待确认点落库结构（SQLite）。

形态：ack 表 + 待确认表（两种允许形态中择一），禁止只在内存比较游标。
  ack_cursors      —— 每运单一行：持久 ACK 游标 ack_seq、正式物化进度 formal_max_seq、版本号
  pending_hops     —— 待 ACK 区：装载时被闸下的点，晋升成功后删除
  formal_hops      —— 正式有效轴视图：仅 retracted=0、primary、已被 ACK 覆盖（seq <= ack_seq）的点
  promotion_proofs —— 独立晋升对照 / 撕裂证明表：运单 + 晋升点 id 列表 + 投影前后 + 正式轴增量

晋升持久化形态（择一）：独立晋升对照 / 撕裂证明表（本实现），不以运单行快照列代替。
提交形态（择一）：同事务删待确认并确保正式轴可见且重算投影，缺一回滚（本实现）。
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ack_cursors (
  waybill_id TEXT PRIMARY KEY REFERENCES waybills(id),
  ack_seq INTEGER NOT NULL DEFAULT 0,
  formal_max_seq INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  held_reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_waybill ON pending_hops(waybill_id, seq);

-- 独立晋升对照 / 撕裂证明表：运单 + 晋升 id 列表 + 投影前后 + 正式轴前后计数。
-- 每次成功晋升写一行，与删除待确认 / 重算投影同事务；回执必须可被本表核验。
CREATE TABLE IF NOT EXISTS promotion_proofs (
  promo_id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  previous_ack_seq INTEGER NOT NULL,
  ack_seq INTEGER NOT NULL,
  promoted_ids_json TEXT NOT NULL,
  promoted_count INTEGER NOT NULL,
  formal_count_before INTEGER NOT NULL,
  formal_max_before INTEGER NOT NULL,
  formal_count_after INTEGER NOT NULL,
  formal_max_after INTEGER NOT NULL,
  projection_before TEXT NOT NULL,
  projection_after TEXT NOT NULL,
  pending_after_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_promotion_waybill ON promotion_proofs(waybill_id);

CREATE VIEW IF NOT EXISTS formal_hops AS
SELECT h.id, h.waybill_id, h.event_ts, h.location_code, h.event_code, h.seq,
       h.channel, h.retracted, c.ack_seq
FROM hops h
JOIN ack_cursors c ON c.waybill_id = h.waybill_id
WHERE h.channel = 'primary'
  AND h.retracted = 0
  AND h.seq IS NOT NULL
  AND h.seq <= c.ack_seq;
"""


def init_ack_schema(conn) -> None:
    """幂等建表 / 视图，并为已存在运单补齐游标行与待确认回填。"""
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO ack_cursors(waybill_id, ack_seq, formal_max_seq, version, updated_at)
        SELECT id, 0, 0, 0, datetime('now') FROM waybills
        """
    )
    # 旧库升级回填：升级前已在 hops 基表、但既不被当前 ACK 覆盖（formal_hops 不可见）
    # 又不在待确认区的点，必须补进 pending_hops，避免落入「两不可查」的中间态。
    conn.execute(
        """
        INSERT OR IGNORE INTO pending_hops(id, waybill_id, event_ts, location_code, event_code, seq, held_reason, created_at)
        SELECT h.id, h.waybill_id, h.event_ts, h.location_code, h.event_code, h.seq,
               CASE WHEN h.seq IS NULL THEN 'missing_seq' ELSE 'over_ack' END,
               datetime('now')
        FROM hops h
        JOIN ack_cursors c ON c.waybill_id = h.waybill_id
        WHERE h.channel = 'primary'
          AND h.retracted = 0
          AND (h.seq IS NULL OR h.seq > c.ack_seq)
          AND NOT EXISTS (SELECT 1 FROM pending_hops p WHERE p.id = h.id)
        """
    )
