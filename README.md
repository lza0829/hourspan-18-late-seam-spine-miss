# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 迟到快照缝链（late_snapshot_apply）

快照 `as_of_ts` 严格早于门店当前镜 `as_of_ts` 即判迟到，入档批自动分流：

- 禁止盖写当前镜：迟到段 `is_current=0`，镜 as_of 永不被拖回，镜仍只指时间轴最新段
- 缝链：按 as_of 切开被跨越旧段（旧段 `valid_to` 收到迟到点），插入迟到段并重接后续
  valid 边界；迟到点早于链首时前置缝入
- 落库形态（择一·甲）：独立审计表 `late_snapshot_audit`（快照 id、原镜 as_of、
  被切 hist_id、缝入 hist_id），+ 缝点（衔接边）表 `late_seam_points`
  （store_id、late_as_of、前段 hist_id、本段 hist_id、后段 hist_id 可空）。
  禁止静默改镜、禁止无审计丢弃、禁止缝成功而无缝点。
- 失败闭（择一·甲）：缝链、审计、缝点同事务，写前内存草稿校验全链区间，写后复验；
  区间重叠/链断裂/缝点缺失或分叉整笔回滚（HTTP 409），杜绝「审计成功但区间重叠」
  与「缝成功而无缝点」。

缝点架构（全局贯彻其一，取【甲】）：

- **甲（采用）以缝点记录导航为准**：跨缝 as-of 点查必须经 `late_seam_points`
  定位；缺缝点记录则点查失败（`found=false, reason=seam_point_missing`）。
  缝点核证须标出缺记录，且不得只靠「缺记录」判整条链失败（链区间复验独立进行）。
- 乙（未采用）以 hist 区间扫描为准，缝点仅留痕，点查裸扫 valid 区间也能命中。
  两者口径可直接区分：删去缝点行后，甲点查失败、乙仍命中。

接口：

- `POST /api/ingest/run` body `{"fixture": "late_snapshot_batch.json"}` —— 迟到批（含正常分流对照）
- `POST /api/ingest/run` body `{"fixture": "late_snapshot_overlap.json"}` —— 重叠夹具，须 409 整笔失败
- `GET /api/late/audit` —— 迟到审计原始行
- `GET /api/late/attest?store_id=` —— 只读核证：原镜 as_of、缝入 hist、现镜 as_of 三方互证 + 全链区间/缝点复验
- `GET /api/late/seam-spine?store_id=` —— 缝点记录列表（前/本/后段 hist + 对齐判定）
- `GET /api/late/seam-attest?store_id=` —— 缝点核证只读接口：缝成功必有点、点与实链/审计三方对齐
- `GET /api/late/seam-lookup?store_id=&as_of=` —— 跨缝点查（架构甲：经缝点记录导航，缺记录即失败）

后端验收（隔离临时库）：`cd backend && python -m unittest discover -s tests`

