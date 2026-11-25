# 古村巡游改线后的排演承诺与现场切换

场地台账记录通路的方向和时段，节目有演员与道具安排。导游团的到站事实不同于席位开放。场务回执分别反映接单和实际执行。

`fixtures/registers.json` 提供演练台账，`fixtures/events.ndjson` 提供各端送达的独立消息，`fixtures/receipts.json` 提供合作方的模拟回执。事件游标为送达顺序。业务发生时点出现在消息内容中时，以其来源所注明的时点为准。整套资料均为合成数据。

## 组件

- `fixture_gateway.py`：合作方联调桩（只读台账/事件流 + 回执协议）。
- `rehearsal/`：导演台改排后端，负责办理改线、占用与对账。
  - `models.py` 领域模型；`ledger.py` 线程安全台账与快照；
  - `engine.py` 可达性、围栏、资源缓冲、承诺审计等规则；
  - `service.py` 应用服务（代次栅栏、现场记录、对账）；
  - `gateway.py` 回执客户端（HTTP / 离线 fixture 两种模式）；
  - `sync.py` 事件游标同步；`server.py` HTTP API。

## 核心规则

1. **改排不是平移街道**：分段方向按时点生效，路线按 BFS 实时计算；允许把巡游段改为定点（`to_fixed`），但承诺的互动内容与观众进入方式必须保留或显式补齐。
2. **同一资源不许两队**：队伍/道具的连续预约要满足换装、移动与道具交接缓冲；两套草稿同时借用同一资源时，先确认的一套生成新版排演单，另一套的代次随即过期，确认时返回 `stale_plan`，必须基于新版重算，不能带旧可用时段生效。
3. **承诺以实际参加为准**：替补节目只能落在事先认可的站点且满足无障碍等进入方式；游客只能原地等候时不得被安排到别的站；只有现场接人记录（`pickup`）才把承诺标记为 `fulfilled`。
4. **已执行不可变**：`team_start` 与 `pickup` 确认过的预约保留，改排触碰即报 `executed_immutable`；未开始的部分可改。
5. **围栏裂脑**：同一分段两端版本/动作不一致时标记 `split_brain`，该边从通行图剔除，任何一端都不能自行推断已开放；导游接口在站点未确认当前版本生效前一律拒绝带队。
6. **断线可恢复**：派发回执按 `reference` 幂等，`lost_reply` 后同号重试；全部状态写入 `--snapshot` 快照，进程重启后从已确认进度继续。
7. **改排失败不影响在办站点**：非法草稿不能确认，当前确认版本的预约继续可执行。

## 本地联调

使用 Python 3.11.2 标准库，只监听本地地址，不需要账号或外部连接。

```sh
# 单元与端到端测试
python3 -m unittest discover -s tests -v
python3 -m unittest -v                       # 联调桩自带测试

# 启动改排后端（离线回执模式，启动时自动摄入 fixture 事件）
python3 -m rehearsal --port 8080 --snapshot state/rehearsal.json

# 对接真实联调桩的回执协议
python3 fixture_gateway.py --port 8765 &
python3 -m rehearsal --port 8080 \
  --receipt-base-url http://127.0.0.1:8765
```

## HTTP API 摘要

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/segments?minute=` | 各分段当前方向与两端围栏（含 `split_brain`） |
| `GET /v1/route?from=&to=&minute=` | 按时刻与围栏计算可达路线 |
| `POST /v1/segments/reports` | 场地提交分段方向与生效时间 |
| `POST /v1/audience/arrivals` | 登记游客到位、原地等候或可带走 |
| `POST /v1/plans` | 提交改排草稿（`move`/`to_fixed`/`substitute`/`cancel`），返回阻断与告警 |
| `POST /v1/plans/{id}/confirm` | 确认草稿；代次过期返回 409 `stale_plan`，非法草稿返回 422 |
| `GET /v1/sheets/current`、`/v1/sheets/{version}` | 查看各版排演单（此刻能照着走的是 current） |
| `GET /v1/resources` | 当前确认版本中各队/道具的占用时段 |
| `POST /v1/progress` | 值班台记录 `team_start` / `pickup` |
| `POST /v1/fences` | 路口按某版本 `released` / `blocked` |
| `POST /v1/distribute` | 向站点派发排演单并取幂等回执（断线可重试） |
| `GET /v1/reconciliation` | 各站收到/生效版本、权威状态、已执行预约与承诺审计 |
| `POST /v1/guide/route` | 导游带队授权：版本未生效、裂脑或等候约束未满足时拒绝 |
| `POST /v1/events/ingest`、`GET /v1/events` | 现场事件按游标摄入与查询 |

## 联调桩协议（fixture_gateway.py）

`GET /v1/registers` 返回台账。用 `GET /v1/events?after=0&limit=50` 读取一页消息，后续页将上一页的 `next_cursor` 作为 `after`。每条消息有 `cursor`、`source`、`minute` 及原业务记录 `payload`。

对端发送协议为 `POST /v1/receipts`，正文提供 `reference`、`operation`、`recipient`，三个字段均为字符串。返回对象含 `reference` 和对端 `receipt`。若条目设置 `lost_reply`，首次调用被记录后连接断开。可用 `GET /v1/receipts/<reference>` 查看已记录的回执。相同编号和内容可重试，编号被另一个操作使用时返回 409。

联调进程运行期间保存回执记录。需要试读其他台账、消息与回执时，可通过 `--fixtures` 指向另一组同名资料文件。合作方只返回模拟事实，业务办理功能由 `rehearsal/` 后端负责。
