# Webview 界面服务进程设计与实现文档 (process_webview)

## 1. 概述与定位

`WebviewApp` 是整个系统面向外部用户、终端命令行工具 (`cli/cli.py`) 和前端可视化界面的**唯一外部通信网关**。
在重构前，Webview 是作为 Platform 进程内部的一个子线程 (`WebviewThread`) 运行；重构后，**Webview 被彻底重构为一个拥有独立内存空间、独立事件循环的独立 OS 进程**。

对外保持全部现有 HTTP REST 接口与 WebSocket 协议 100% 行为不变；对内彻底废弃 HTTP，全部通过 `multiprocessing.Pipe` 管道与其他进程协同。

---

## 2. 职责与功能

1. **HTTP REST 服务与 Web 静态资源托管**：托管地图前端页面 (`index.html`)、静态脚本与样式 (`static/app.js`)，暴露增删改查、机场与系统状态接口。
2. **WebSocket 实时双向流**：维护前端浏览器客户端连接池，在仿真器运行时以 0.5s/tick 周期向前端实时推流空域全部物体运动学姿态与坐标。
3. **系统状态持久化管理**：维护 `src/platform/state.json`，持久化记录机场经纬度、跑道方向及 AI 管制员启用开关。
4. **内部智能体代理网关**：
   - 接收外部对 `/api/controller/test_ai` 的请求，通过管道直接与 Controller 进程交互并返回测试数据。
   - 接收外部对 `/api/pilot/test_ai` 和 `/api/pilot/radio_RX` 的请求，通过管道直接与 Pilot 进程交互并返回执行结果。
5. **内部环境数据供给中继**：
   - 为 Controller 提供 `get_airport` 和 `get_controller_state` 管道接口。
   - 接收 Controller 与 Pilot 进程通过管道发送过来的无线电管制日志 (`add_radio_log`)，并实时下发至前端 WebSocket 终端显示。

---

## 3. 面向对象设计

核心实现位于 `src/platform/webview.py` 中的 `WebviewApp` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `host` | `str` | 绑定的监听地址（默认 `0.0.0.0`） |
| `port` | `int` | 绑定的监听端口（默认 `8000`） |
| `app` | `FastAPI` | FastAPI 应用程序实例 |
| `clients` | `List[WebSocket]` | 已连接的 WebSocket 前端客户端活跃连接列表 |
| `sync_task` | `Optional[asyncio.Task]` | 0.5 秒一次的状态同步广播协程任务 |
| `ch_simulator` | `Optional[IPCChannel]` | 连接至 Simulator 进程的双向全双工管道通道 |
| `ch_controller` | `Optional[IPCChannel]` | 连接至 Controller 进程的双向全双工管道通道 |
| `ch_pilot` | `Optional[IPCChannel]` | 连接至 Pilot 进程的双向全双工管道通道 |
| `ch_master` | `Optional[IPCChannel]` | 连接至 Master 进程的管理双向全双工管道通道 |
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
  - 说明: 保存机场坐标至 `state.json` 并向所有 WebSocket 客户端广播 `airport` 消息。

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

#### 7. 无线电日志记录
- **路径**: `POST /api/radio_log`
  - JSON Body: `{"source": str, "type": str, "text": Optional[str], "data": Optional[dict]}`
  - 说明: 向所有 WebSocket 客户端广播无线电文字/语音及解算参数日志。

### 4.2 WebSocket 接口 (`/ws`)

- **连接建立**:
  - 自动向新客户端依次下推当前全量数据：`state`（所有物体）、`airport`（机场数据）、`controller_state`（管制员开关状态）、`simulator_state`（模拟器运行状态）。
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

---

## 5. 对内开放的管道接口 (Internal IPC Interfaces)

WebviewApp 实例在其内部 IPC 通道上向其他进程开放了以下处理函数：

| Action | 监听通道 | 说明 |
| :--- | :--- | :--- |
| `get_airport` | `ch_controller` | 返回当前保存的机场经纬度与跑道方向。 |
| `get_controller_state` | `ch_controller` | 返回当前 AI 管制员使能开关布尔值。 |
| `add_radio_log` | `ch_controller`, `ch_pilot` | 接收无线电文本或提取的参数字典，打包并向前端 WebSocket 广播。 |
| `get_objects` | `ch_controller` | 中继查询当前空域所有物体的运动学数据。 |
