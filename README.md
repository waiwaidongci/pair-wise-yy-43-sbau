# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/voyage_rules.py`：出航窗口判定（海况/回收量阈值、船期重叠、状态守卫）。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 出航窗口台账（围控转入回收后）

- `POST /api/items/{id}/voyage-windows`：登记船名、离港/回港时刻（含往返和卸油时间）、预计回收量、海况。`operations`或`response_commander`。
  - 海况超过四级，或预计回收量不到事件油量一成：窗口状态为`pending`留待重排，`hold_reason`说明原因；复测后调`POST /api/voyage-windows/{id}/reconfirm`重新确认。
  - 同一回收船与未回港窗口（含留待重排）船期重叠返回`409`，响应体`details.conflicts`给出冲突安排；前窗回港时刻等于后窗离港时刻不算重叠。
  - 正常登记直接进入`confirmed`。
- `POST /api/voyage-windows/{id}/return`：回港登记实际回收量、分离纯油量、分离水量和油水分离结果，状态变`completed`并自动补一条已关闭回收记录，此后才计入进度。
- `GET /api/voyage-windows?status=pending|confirmed|completed&item_id=`：台账列表，可按状态筛选。
- `GET /api/voyage-windows/{id}`：窗口详情。
- `GET /api/items/{id}/voyage-progress`：回收进度，详情含三类数量——`estimated_qty`（预计回收量）、`recovered_qty`（实际回收量）、`separated_oil_qty`（油水分离后纯油量）。

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
