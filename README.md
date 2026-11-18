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
| `GET /api/pending-hops[?waybill_id=]` | 待 ACK 区查询 |
| `POST /api/waybills/{id}/ack` | ACK 推进入口（body `{"ack_seq": n}`；下调返回 409） |
| `GET /api/ack-demo/ordering` | 先装载后 ACK vs 先 ACK 后装载 顺序对照（独立临时库） |
| `GET /api/waybills/{id}/timeline` | 正式轴、待确认点、游标、分叉证明同返 |

前端「ACK 游标」页看证明 / 待确认 / 顺序对照；测绘板检查器可推进 ACK；装载页展示正式 / 待确认分流计数。

后端测试：`cd backend && python -m pytest tests/`。

