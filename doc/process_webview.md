# Webview 界面服务进程设计与实现文档 (process_webview)

## 1. 概述与定位

`WebviewApp` 是整个系统面向外部用户、终端命令行工具 (`cli/cli.py`) 和前端可视化界面的**唯一外部通信网关**。
在重构前，Webview 是作为 Platform 进程内部的一个子线程 (`WebviewThread`) 运行；重构后，**Webview 被彻底重构为一个拥有独立内存空间、独立事件循环的独立 OS 进程**。

对外保持全部现有 HTTP REST 接口与 WebSocket 协议 100% 行为不变；对内彻底废弃 HTTP，全部通过 `multiprocessing.Pipe` 管道与其他进程协同。

---

## 2. 职责与功能

1. **HTTP REST 服务与 Web 静态资源托管**：托管地图前端页面 (`index.html`)、静态脚本与样式 (`static/app.js`)，暴露增删改查、机场与系统状态接口。
2. **WebSocket 实时双向流**：维护前端浏览器客户端连接池，在仿真器运行时以 0.5s/tick 周期向前端实时推流空域全部物体运动学姿态与坐标。
3. **场景状态持久化与快照管理**：支持将当前机场坐标、跑道方向及空域全部飞机的完整飞行状态（坐标、航速、航向、高度、垂直速度、转向率、地面/空中状态 status 等）显式保存至 `state.json`，并支持从 `state.json` 读取数据完整还原场景。**启动时服务解耦与 `state.json`，不再自动读取加载，保持初始干净空域**。
4. **内部智能体代理网关**：
   - 接收外部对 `/api/controller/test_ai` 的请求，通过管道直接与 Controller 进程交互并返回测试数据。
   - 接收外部对 `/api/pilot/test_ai` 和 `/api/pilot/radio_RX` 的请求，通过管道直接与 Pilot 进程交互并返回执行结果。
5. **内部环境数据供给与无线电专线展示网关**：
   - 为 Controller 提供 `get_airport` 与 `get_objects` 飞行数据管道接口；通过管道向 Controller 进程发起 `get_controller_state` 与 `set_controller_state` 查询与控制。
   - **无线电专线接入**：通过独立的无线电专线通道 (`ch_radio_sim`) 接收来自模拟器中继的无线电语音报文 (`add_radio_log`)，存入内存纯净历史缓冲区并实时下发至前端 WebSocket 终端与 CLI 接口。**飞行数据专线严禁挂载无线电动作**。

---

## 3. 面向对象设计

核心实现位于 `src/platform/webview.py` 中的 `WebviewApp` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `host` | `str` | 绑定的监听地址（默认 `0.0.0.0`） |
| `port` | `int` | 绑定的监听端口（默认 `8000`） |
| `app` | `FastAPI` | FastAPI 应用程序实例 |
| `current_airport` | `Dict[str, Any]` | 内存当前配置的机场地理坐标及跑道方向（初始为空） |
| `clients` | `List[WebSocket]` | 已连接的 WebSocket 前端客户端活跃连接列表 |
| `sync_task` | `Optional[asyncio.Task]` | 0.5 秒一次的状态同步广播协程任务 |
| `ch_flight_sim` | `Optional[IPCChannel]` | 飞行数据专线：连接至 Simulator 进程（CRUD、仿真启停） |
| `ch_flight_ctrl` | `Optional[IPCChannel]` | 飞行数据专线：连接至 Controller 进程（控制/查询管制员开关、代理 `test_ai`） |
| `ch_flight_pilot` | `Optional[IPCChannel]` | 飞行数据专线：连接至 Pilot 进程（代理 `test_ai`、测试） |
| `ch_flight_master` | `Optional[IPCChannel]` | 系统管理专线：连接至 Master 进程（生命周期与日志） |
| `ch_radio_sim` | `Optional[IPCChannel]` | **无线电通信专线**：连接至 Simulator 进程，专职接收纯净无线电通话 (`add_radio_log`) |
| `local_simulator` | `Optional[AirplaneSimulator]` | 本地后备模拟器实例（用于无管道单进程测试环境） |


---

## 4. 对外开放的网络接口 (External Interfaces)

Webview 严格保持供 CLI 和 Web 前端使用的所有外部接口规范：

### 4.1 HTTP REST 接口

#### 1. 首页静态页面
- **路径**: `GET /`
- **说明**: 返回注入版本时间戳防缓存机制的 Leaflet 实时空域地图 HTML 页面。

#### 2. 机场配置接口
- **路径**: `GET /api/airport`
  - 返回: `{"lat": float, "lon": float, "runway_heading": float}`
- **路径**: `POST /api/airport`
  - 参数 (JSON Body): `{"lat": float, "lon": float, "runway_heading": Optional[float]}`
  - 说明: 更新内存当前机场坐标，同步至模拟器并向所有 WebSocket 客户端广播 `airport` 消息（不自动覆写 `state.json`）。

#### 3. AI 管制员启用开关
- **路径**: `GET /api/controller_state`
  - 返回: `{"enabled": bool}`
- **路径**: `POST /api/controller_state`
  - 参数 (JSON Body): `{"enabled": bool}`
  - 说明: 切换 AI 管制员是否监控空域并向所有 WebSocket 客户端广播 `controller_state` 消息。

#### 4. 飞行模拟器状态控制
- **路径**: `GET /api/simulator/state`
  - 返回: `{"running": bool}`
- **路径**: `POST /api/simulator/state`
  - 参数 (JSON Body): `{"running": bool}`
  - 说明: 控制启动/停止模拟器物理积分循环，向前端广播 `simulator_state` 消息。
- **路径**: `POST /api/simulator/start`
  - 说明: 便捷端点，启动模拟器推算。
- **路径**: `POST /api/simulator/stop`
  - 说明: 便捷端点，停止模拟器推算。

#### 5. 飞行器物体增删改查
- **路径**: `GET /api/objects`
  - 返回: `List[Dict]`，全量物体运动姿态快照。
- **路径**: `GET /api/add`
  - Query 参数: `name`, `lat`, `lon`, `speed`, `heading`, `altitude`, `target_heading`, `target_altitude`
  - 说明: 向模拟器添加新飞行器，触发 WebSocket 状态广播，返回新物体对象。
- **路径**: `GET /api/update`
  - Query 参数: `id`, `speed`, `heading`, `altitude`
  - 说明: 更新飞行器目标参数，触发 WebSocket 状态广播。
- **路径**: `GET / POST / DELETE /api/delete`
  - 参数: Query 或 JSON Body 中的 `id`
  - 说明: 从模拟器中移除指定 UUID 的飞行器，触发 WebSocket 状态广播。

#### 6. 代理智能体接口
- **路径**: `GET / POST /api/controller/test_ai`
  - Query 参数: `timeout` (float, 默认 30.0)
  - 说明: 内部通过管道调用 Controller 的 `test_ai` 接口并返回大模型连通性与延迟数据。
- **路径**: `GET / POST /api/pilot/test_ai`
  - Query 参数: `timeout` (float, 默认 30.0)
  - 说明: 内部通过管道调用 Pilot 的 `test_ai` 接口并返回大模型连通性与延迟数据。
- **路径**: `POST /api/pilot/radio_RX`
  - JSON Body: `{"text": "指令文本"}`
  - 说明: 内部通过管道调用 Pilot 的 `radio_RX` 接口解析自然语言指令并更新目标飞行器状态。

#### 7. 无线电日志记录与查询
- **路径**: `POST /api/radio_log`
  - JSON Body: `{"source": str, "type": Optional[str], "text": Optional[str], "data": Optional[dict]}`
  - 说明: 向内存历史缓冲区 (`self.radio_logs`) 记录日志，并向所有 WebSocket 客户端广播无线电文字/语音及解算参数日志。
- **路径**: `GET /api/radio_log` / `GET /api/radio_logs`
  - Query 参数: `limit` (int, 默认 50), `offset` (int, 默认 0)
  - 返回: `{"status": "success", "total": int, "limit": int, "offset": int, "logs": List[Dict]}`
  - 说明: **CLI 工具与外部客户端读取无线电通讯历史的唯一标准接口**。支持基于偏移量与条数的分页查询，按时间先后顺序返回记录。

#### 8. 场景状态持久化与快照管理接口
- **路径**: `GET /api/state`
  - 返回: `{"status": "success", "airport": dict, "objects_count": int, "objects": List[Dict], "controller_enabled": bool}`
  - 说明: 获取当前内存中场景状态快照（包括当前配置的机场地理坐标、所有飞机的实时运动学状态与 AI 管制员开关）。
- **路径**: `POST /api/state/save`
  - 返回: `{"status": "success", "message": str, "airport": dict, "objects_count": int, "objects": List[Dict]}`
  - 说明: 将当前所有机场坐标、飞机坐标、全套飞行状态参数（航速、航向、高度、垂直速度、转向率、目标航向/高度、`status` 地面/空中状态）保存至 `state.json`。
- **路径**: `POST /api/state/load` / `POST /api/state/restore`
  - 返回: `{"status": "success", "message": str, "airport": dict, "objects_count": int, "objects": List[Dict]}`
  - 说明: 从 `state.json` 读取数据，还原机场坐标与全部飞机的飞行状态，并即时通过 WebSocket 向雷达前端广播，重绘空域。
- **路径**: `POST /api/reset`
  - 返回: `{"status": "success", "message": str, "airport": dict, "objects_count": int, "objects": List, "simulator_running": bool, "controller_enabled": bool}`
  - 说明: 重置空域场景：通知模拟器侧删除全部飞机与机场基准点，停止模拟器物理推算 (`running = False`)，停止 AI 管制员调度 (`enabled = False`)，并通过 WebSocket 广播清空事件。

### 4.2 WebSocket 接口 (`/ws`) 与前端界面交互

- **连接建立**:
  - 自动向新客户端依次下推当前全量数据：`state`（所有物体）、`airport`（机场数据）、`controller_state`（管制员开关状态）、`simulator_state`（模拟器运行状态）、**最近 30 条无线电历史日志** (`radio_log`)。
- **客户端上传事件**:
  - `{"type": "add", ...}`: 添加新飞机。
  - `{"type": "update", ...}`: 更新飞机参数。
  - `{"type": "remove", "id": "..."}`: 移除飞机。
- **下行广播类型**:
  - `state`: 全量物体最新坐标与状态。
  - `airport`: 机场配置变更。
  - `controller_state`: 管制员开关变更。
  - `simulator_state`: 模拟器启停状态变更。
  - `radio_log`: 管制员或飞行员的无线电交互日志。
- **前端操作按钮**:
  - 右侧控制面板提供 **保存状态** 与 **读取状态** 按钮，分别调用 `/api/state/save` 与 `/api/state/load` 接口；
  - 提供 **重置场景 (删除全部飞机与机场并停止)** 按钮，调用 `/api/reset` 清空空域并停机；
  - 提供 **添加物体**、**设置机场**、**飞行模拟器开关**、**AI管制员开关** 控制按钮。
- **CLI 终端命令支持**:
  - 支持 `cli save`、`cli load` 管理 `state.json` 场景持久化，`cli state` 查看当前空域飞行状态；
  - 支持 `cli reset` 彻底重置场景并停止模拟器和管制员；
  - 交互式控制台菜单分别提供独立功能入口：`[6] State`（查看飞行状态）、`[7] Save / Load`（持久化读写）、`[8] Reset`（空域重置）。

---

## 5. 对内开放的管道接口 (Internal IPC Interfaces)

WebviewApp 严格区分两套管道并开放以下处理函数：

### 5.1 飞行数据队列接口 (Flight Data IPC Queue)
监听通道 `ch_flight_ctrl`（与 Controller 连接），严禁挂载无线电动作：

| Action | 监听通道 | 说明 |
| :--- | :--- | :--- |
| `get_airport` | `ch_flight_ctrl` | 返回当前保存的机场经纬度与跑道方向。 |
| `get_objects` | `ch_flight_ctrl` | 中继向模拟器查询当前空域所有物体的运动学数据。 |

### 5.2 无线电通信专线接口 (Radio IPC Queue)
专用于监听来自模拟器中继的无线电语音通话：

| Action | 监听通道 | 说明 |
| :--- | :--- | :--- |
| `add_radio_log` | `ch_radio_sim` | 接收模拟器专线中继的无线电自然语言语音报文，存入纯净历史缓冲区并向前端 WebSocket 广播。 |


---

## 6. 日志系统集成说明

本进程不直接创建本地独立日志文件：
1. 实例初始化时通过 `setup_child_logging(channel=self.ch_master, process_name="webview")` 配置 `IPCLogHandler`。
2. 进程内产生的 Webview 服务、API 请求与 WebSocket 连接日志统一通过 `ch_master` 异步上报至 Master 进程，由 Master 统一汇聚写入 `logs/master.log` 并支持日志轮转。
3. 若无 Master 管道连接（如单体测试环境），自动安全回退至本地控制台输出，确保进程不会因日志原因阻塞挂起。
