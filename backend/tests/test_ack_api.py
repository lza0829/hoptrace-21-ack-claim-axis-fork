"""路由 / 端到端测试：ACK 入口、装载闸、时间线、分叉证明接口协同。"""


def test_overview_exposes_pending_counter(client):
    data = client.get("/api/overview").json()
    assert data["hops_primary"] == 0          # 初始 ACK=0，无正式点
    assert data["hops_pending_ack"] == 4      # 种子四点全在待 ACK 区


def test_timeline_shows_formal_and_pending_separately(client):
    body = client.get("/api/waybills/WB-1001/timeline").json()
    assert body["hops"] == []
    assert {p["id"] for p in body["pending_hops"]} == {"H-1", "H-2", "H-3"}
    assert body["ack_cursor"]["ack_seq"] == 0
    assert body["fork_proof"]["conclusion"].startswith("受控分叉")
    assert body["fork_proof"]["pending_keys"] == ["H-1", "H-2", "H-3"]


def test_ack_advance_endpoint_promotes_and_proofs(client):
    r = client.post("/api/waybills/WB-1001/ack", json={"ack_seq": 2})
    assert r.status_code == 200
    payload = r.json()
    assert sorted(payload["promoted"]) == ["H-1", "H-2"]
    assert payload["fork_proof"]["formal_max_seq"] == 2
    assert payload["fork_proof"]["pending_keys"] == ["H-3"]

    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in tl["hops"]] == ["H-1", "H-2"]
    assert [p["id"] for p in tl["pending_hops"]] == ["H-3"]

    proof = client.get("/api/ack-fork-proofs?waybill_id=WB-1001").json()
    assert proof["ack_seq"] == 2
    assert proof["formal_max_seq"] == 2
    assert proof["over_ack_formal_count"] == 0


def test_ack_downgrade_returns_409_and_changes_nothing(client):
    client.post("/api/waybills/WB-1001/ack", json={"ack_seq": 3})
    r = client.post("/api/waybills/WB-1001/ack", json={"ack_seq": 1})
    assert r.status_code == 409
    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert tl["ack_cursor"]["ack_seq"] == 3
    assert len(tl["hops"]) == 3
    assert tl["pending_hops"] == []


def test_ingest_batch_gates_by_ack_and_is_idempotent(client):
    # WB-1002 当前 ACK=0：装载 H-6(seq2) 进待确认
    r = client.post("/api/ingest/run")
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["formal"] == 0
    assert report["pending_ack"] == 2
    pending = client.get("/api/pending-hops").json()
    assert {p["id"] for p in pending} >= {"H-5", "H-6"}

    # 推进 ACK 覆盖 H-6（WB-1002 seq2）
    client.post("/api/waybills/WB-1002/ack", json={"ack_seq": 2})
    tl = client.get("/api/waybills/WB-1002/timeline").json()
    assert [h["id"] for h in tl["hops"]] == ["H-4", "H-6"]

    # 幂等重放：同批次再跑，不产生重复分流
    r2 = client.post("/api/ingest/run").json()["report"]
    assert r2["duplicate"] == 2
    assert r2["written"] == 0


def test_load_first_vs_ack_first_distinction_demo(client):
    data = client.get("/api/ack-demo/ordering").json()
    assert len(data["scenarios"]) == 2
    load_first, ack_first = data["scenarios"]
    # 先装载后 ACK：装载瞬间正式轴为空，四点待确认
    after_load_a = load_first["steps"][1]["proof"]
    assert after_load_a["formal_count"] == 0
    assert len(after_load_a["pending_keys"]) == 4
    # 先 ACK 后装载：装载瞬间四点全在正式轴
    after_load_b = ack_first["steps"][2]["proof"]
    assert after_load_b["formal_count"] == 4
    assert after_load_b["pending_keys"] == []
    # 最终两边对齐
    assert load_first["steps"][-2]["proof"]["formal_max_seq"] == 4
    # 下调在演示路径中被拒绝
    downgrade_step = load_first["steps"][-1]
    assert downgrade_step["result"]["rejected"] is True
    assert downgrade_step["proof"]["ack_seq"] == 4


def test_fork_proof_listing_covers_all_waybills(client):
    rows = client.get("/api/ack-fork-proofs").json()
    ids = {r["waybill_id"] for r in rows}
    assert {"WB-1001", "WB-1002"} <= ids
    for r in rows:
        assert set(["waybill_id", "ack_seq", "formal_max_seq", "pending_keys", "conclusion"]) <= set(r)


def test_ack_unknown_waybill_404(client):
    assert client.post("/api/waybills/NOPE/ack", json={"ack_seq": 1}).status_code == 404
    assert client.get("/api/ack-fork-proofs?waybill_id=NOPE").status_code == 404
