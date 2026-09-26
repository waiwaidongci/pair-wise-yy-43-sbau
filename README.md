# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
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
- `POST /api/items/{id}/windows`，登记航窗口（船名、离港/回港时刻、预计回收量、海况）
- `GET /api/items/{id}/windows?status=`
- `GET /api/windows?status=&item_id=&ship_name=`
- `POST /api/windows/{id}/confirm`，复测后重新确认留待重排的窗口
- `POST /api/windows/{id}/return`，回港登记回收量与油水分离结果
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 航窗口台账

事件进入围控（`containing`）后才可登记航窗口；窗口覆盖离港到回港全程（含往返航行与卸油），同一艘船的已排期窗口时间重叠时返回`409`及冲突安排。判定在`src/rules.py`，台账在`src/repository.py`，请求入口在`src/http_api.py`。

- 海况超过四级或预计回收量不足事件油量一成时，窗口记为`pending_reschedule`（留待重排）并登记原因；复测后通过`confirm`重新提交时刻、海况和预计回收量，再次判定与查冲突。
- 回港登记实际回收量和油水分离结果后窗口转为`completed`，才计入进度；事件详情的`recovery_progress`给出三类数量：预计回收、实际回收、分离净油。
- 台账列表支持按`status`（scheduled / pending_reschedule / completed）等条件查看。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
