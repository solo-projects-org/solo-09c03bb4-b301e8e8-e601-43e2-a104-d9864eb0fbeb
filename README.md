# 单断面总磷核查 API

FastAPI + SQLite 实现的单断面总磷（TP）核查服务。化验单以整数 μg/L 声明浓度区间，
服务按水样汇总现行区间，并给出月度警戒线计数的上下界与待复测名单。

## 规则映射

| 业务规则 | 实现 |
| --- | --- |
| 化验单填浓度闭区间 | `measured: {lower, upper}`，两端均含，`lower <= upper`，均为非负整数 |
| 未检出填检出限，按 `[0, L)` 计 | `nondetect: {detection_limit: L}`，内部记为 `[0, L)`；整数域等价于 `[0, L-1]` |
| 每张单只用最高修订 | 每份 `(report_no)` 取 `MAX(revision)` 参与交集，旧修订仅保留历史 |
| 原样重报不增计 | 同单号、同修订、同内容返回 `200`，`accepted=false, idempotent=true`，不新增行、不重算 |
| 同号同版异文退回 | 同 `report_no` + `revision` 但内容不同返回 `409` |
| 旧修订不可补录 | 提交修订号不大于已存最高修订且非幂等时返回 `409` |
| 同号跨水样 | 化验单号全局唯一，重复用于其他水样返回 `409` |
| 同水样取各现行区间交集 | `sample_state` 保存交集；空交集记为 `retest` |
| 交集为空列为待复测、不计入分母 | `retest` 水样进入名单并从 `denominator` 排除 |
| 采样月固定 | `samples.sampling_month` 创建后不可变更，冲突返回 `409` |
| 警戒线计数上下界 | 下界=区间下端点已达警戒线的样本数（必然达到）；上界=区间上端点可达警戒线的样本数（可能达到） |
| 更正只重算本水样 | 写事务内先减去该水样旧贡献、再加入新贡献，不扫描其他水样 |
| 三件产物一起生效 | 化验单、`sample_state`、月度摘要/直方图在同一 `BEGIN IMMEDIATE` 事务提交 |
| 写入失败仍查原结果 | 任何异常整体回滚，返回 `500`，读路径只看到上次已提交状态 |
| 一次查询来自同一已提交状态 | `/summary` 在单个读事务（WAL 快照）内取计数与名单，并校验 `denominator + retest_count == total_samples` 且 `retest_count == len(list)` |

### 区间端点范围

浓度端点与警戒线均为非负整数，上限为 SQLite 的 `2^63-1`；超出范围的端点直接返回 `422`，
避免 SQLite 静默转成浮点后丢失整数语义。浮点数（如 `10.5`）同样被拒绝。

### 上下界语义

设某水样现行区间为 `[lo, hi]`（未检出为 `[0, L)`，`hi = L-1`），警戒线 `T`：

- 下界（必然 ≥ T）：`lo >= T`；
- 上界（可能 ≥ T）：`hi >= T`。

因为 `[0, L)` 不含 `L`，警戒线恰好等于 `L` 时计为**未达到**；等于 `L-1` 时计为可能达到。
闭区间则包含端点，`hi == T` 计为达到。

## 启动

```bash
docker compose up --build
```

- Swagger UI：<http://localhost:8000/docs>（每个端点均可 "Try it out"）
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/healthz>
- 数据卷：`tp-data` 挂载到容器 `/data/tp.sqlite3`

本地直接运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TP_DB_PATH=/tmp/tp.sqlite3 uvicorn app.api:app --reload
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/samples` | 登记水样与固定采样月（幂等） |
| `GET` | `/samples/{sample_no}` | 查看水样、化验单历史、现行区间与状态 |
| `POST` | `/samples/{sample_no}/reports` | 提交/更正一张化验单的一个修订 |
| `GET` | `/summary?month=YYYY-MM&threshold=T` | 月度上下界计数、分母与待复测名单 |
| `GET` | `/retest?month=YYYY-MM` | 该月待复测水样号 |
| `POST` | `/band-checks` | 核对各浓度档申报数能否由当月入选水样支撑；可填满时出回执 |
| `GET` | `/band-checks/{check_id}` | 凭回执号查原分档，并判断回执是否仍可用 |
| `GET` | `/healthz` | 健康检查 |

### 示例

```bash
# 登记水样
curl -X POST localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_no":"S-001","sampling_month":"2026-11"}'

# 实测闭区间
curl -X POST localhost:8000/samples/S-001/reports \
  -H 'Content-Type: application/json' \
  -d '{"report_no":"LAB-A-1","revision":1,"result":{"kind":"measured","lower":30,"upper":45}}'

# 未检出，按 [0,10) 计
curl -X POST localhost:8000/samples/S-001/reports \
  -H 'Content-Type: application/json' \
  -d '{"report_no":"LAB-A-2","revision":1,"result":{"kind":"nondetect","detection_limit":10}}'

# 查询月度摘要（该水样交集为空 -> 待复测，分母不计数）
curl 'localhost:8000/summary?month=2026-11&threshold=20'
```

### 分档核验（`/band-checks`）

月报送审前，统计员需要确认各浓度档的申报数能由同一批水样支撑。请求给出采样月、
最多 12 条严格递增的正整数分界线和各档非负整数申报数：

```bash
curl -X POST localhost:8000/band-checks \
  -H 'Content-Type: application/json' \
  -d '{"month":"2026-11","boundaries":[10,20,50],"counts":[1,2,1,1]}'
```

- 分界线把整数浓度从 0 起切成左闭右开的档：`[0,10)`、`[10,20)`、`[20,50)`、
  `[50,+inf)`；`counts` 必须恰好 `len(boundaries)+1` 项，末档无上限。
- 入选水样＝该采样月下现行区间非空的水样，与 `/summary` 的 `denominator` 同源；
  待复测（交集为空）与未出单（无化验单）水样排除在外。判定在同一已提交快照内完成。
- 每个入选水样按其现行整数区间可落在一个或多个连续档（区间内任一整数所在档）。
- 三种 `outcome`：

| outcome | 含义 | 产物 |
| --- | --- | --- |
| `filled` | 申报总数＝入选水样数，且能逐样本分档、各档恰好达到申报数 | `check_id` 回执 + `assignments` 逐样本分档 |
| `count_mismatch` | 总数不符 | `difference = eligible_total - declared_total`，不存回执 |
| `infeasible` | 总数相符但无法分档 | `shortfall`：容量不足的那段连续档、其容量、只能落在这段的水样号，不存回执 |

`infeasible` 的定位依据是区间结构的 Hall 条件：若存在某段连续档 `[i,j]`，其容量
小于「只能落在这段」的水样数，则必然无法分档；服务报告最短的这类段落。示例：

```json
{"outcome":"infeasible","declared_total":4,"eligible_total":4,"difference":0,
 "shortfall":{"first_band":1,"last_band":2,"notation":"[0, 20)",
              "capacity":2,"sample_count":3,"deficit":1,
              "sample_nos":["L1","L2","M1"]}}
```

凭回执号查询：

```bash
curl 'localhost:8000/band-checks/BC-202611-0001'
```

回执保存了出单时每个入选水样的可落档区间快照。`usable=true` 仅当当月入选水样集合
与每个水样的可落档区间**仍与核验时一致**；否则 `recheck_required=true`，并在
`changes.added / removed / changed` 中列出差异（新增、失效、可落档区间移动），提示重核。
区间收窄但可落档不变时，回执仍然可用。回执按 `BC-YYYYMM-NNNN` 编号，按月递增。

## 数据结构

- `samples`：水样号、固定采样月；
- `reports`：`(report_no, revision)` 主键，原始区间端点与内容指纹；
- `sample_state`：每水样一行，`ok` 保存现行交集，`retest` 表示空交集；
- `monthly_summary`：每月总样本数、分母、待复测数；
- `monthly_lower_hist` / `monthly_max_hist`：按区间下端点 / 最大可达整数分桶的计数，
  使任意警戒线的上下界计数为一次索引范围求和，无需扫描水样；
- `band_checks`：分档核验回执，保存按月编号、分界线、申报数与总数；
- `band_check_samples`：回执内逐水样的落档、可落档区间快照与出单时现行区间。

摘要采用增量维护：每个水样对月度聚合最多贡献两条直方图记录，更正时先撤销旧贡献再加入
新贡献，因此只触及目标水样。

## 测试

```bash
pip install -r requirements-dev.txt
TP_DB_PATH=/tmp/unused.sqlite3 pytest tests -q
```

覆盖区间代数（含 `[0,L)` 端点）、幂等与冲突、最高修订、交集与待复测、分母与上下界、
更正局部性（仅重算目标水样）、跨月隔离、写入回滚与恢复、读快照一致性、多线程并发聚合一致性，
以及增量聚合与暴力重算的随机对拍。

分档核验另有：分档代数（左闭右开、末档无上限）、EDF 分档与 Hall 容量不足判定对穷举
全排列的对拍、三种 outcome、回执编号、回执可用性与更正后的重核判定、并发取号唯一性、
请求校验（非递增/超 12 条/浮点/布尔/负数）与 OpenAPI 可试性。

当前共 56 个测试，全部通过。

## 已知限制

- 单进程/单机 SQLite（WAL）。未做跨机复制；如需水平扩展应换 PostgreSQL。
- `threshold` 为查询参数，不做持久化配置。
- 不做化验单文件上传或 OCR，仅接收结构化 JSON。
- Docker 镜像未在本次环境实测（本机无 Docker 守护进程），Compose 与 Dockerfile 已做静态校验。
