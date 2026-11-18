# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## ACK 游标分叉治理（ack_cursor_tear）

源端 ACK 游标与正式时间线物化进度的分叉由持久化闸口约束，禁止只在内存比较游标。

**落库形态（ACK 表 + 待确认表）**

| 表 / 视图 | 作用 |
| --- | --- |
| `ack_cursors` | 每运单一行：持久 `ack_seq`、正式物化进度 `formal_max_seq`、版本号 |
| `pending_hops` | 待 ACK 区：超 ACK 或无业务序号的点，晋升成功后删除 |
| `formal_hops`（视图） | 正式有效轴：仅 `seq <= ack_seq` 的 primary 未撤回点 |
| `promotion_proofs` | 独立晋升对照 / 撕裂证明表：运单 + 晋升 id 列表 + 投影前后 + 正式轴前后计数 |

**晋升提交形态（择一：本实现取前者，失败闭）**

同事务完成「删待确认 → 校验晋升点已在正式有效轴可查 → 重算 `formal_max_seq`
→ 重算状态投影 → 写晋升回执 → 不变量校验」，任一步缺失或校验失败整趟回滚：

- 回执列出晋升点而正式轴点数未增 → 抛错回滚（禁止「宣告已晋升、正式轴没接上」）；
- 正式轴与末条回执一致但投影仍停在晋升前 → 抛错回滚；
- 晋升中途失败 → 待确认集合、正式轴、投影一并保持晋升前，不写回执。

**闸路径（装载时按 ACK 分流，失败闭）**

- 装载事务内逐点判定：`seq <= ack_seq` 进正式轴，否则写 `pending_hops`；无序号点同样进待确认区。
- 提交前 `assert_no_tear()` 校验硬不变量，任一失败整趟回滚。
- ACK 上调：先写持久 ACK，再晋升待确认中已覆盖的点，重算 `formal_max_seq` 与状态投影。
- ACK 下调：整笔拒绝（HTTP 409），不改已正式点与已写 ACK。
- 状态投影只由 `formal_hops` 末点推导，待确认点不参与投影。

**接口**

| 方法 路径 | 说明 |
| --- | --- |
| `GET /api/ack-fork-proofs[?waybill_id=]` | ACK 分叉证明只读接口：运单、ack_seq、正式最大序号、待确认键集合、结论 |
| `GET /api/promotion-tear-audits[?waybill_id=]` | **晋升撕裂核证（只读）**：回执逐条对账正式轴可见性 / 正式轴点数增量 / 待确认是否被无回执清空 / 投影是否停在晋升前 |
| `GET /api/promotion-proofs[?waybill_id=]` | 晋升回执列表（独立晋升对照 / 撕裂证明表） |
| `GET /api/pending-hops[?waybill_id=]` | 待 ACK 区查询 |
| `POST /api/waybills/{id}/ack` | ACK 推进入口（body `{"ack_seq": n}`；下调返回 409），响应含晋升回执 id 与撕裂核证 |
| `GET /api/ack-demo/ordering` | 先装载后 ACK vs 先 ACK 后装载 顺序对照（独立临时库） |
| `GET /api/promotion-demo/tear` | 先装超游标点再上调 ACK 的成功 / 失败对照（夹具 `promotion_tear.json`，独立临时库） |
| `GET /api/waybills/{id}/timeline` | 正式轴、待确认点、游标、分叉证明、晋升撕裂核证同返 |

前端「ACK 游标」页看分叉证明 / 晋升回执 / 撕裂核证 / 待确认 / 两组对照演示；测绘板检查器可推进 ACK 并即时展示末条晋升回执与核证结论；装载页展示正式 / 待确认分流计数。

后端测试：`cd backend && python -m pytest tests/`。

