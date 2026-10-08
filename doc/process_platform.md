# 进程分析：Platform 仿真底座与平台服务进程（含 Webview 线程与 airplane_simulator 线程）

## 1. 进程概览

- **进程名称**：`platform`
- **进程主入口**：[`src/platform/main.py`](file:///home/mickey/git/test_of_realtime_AI_agent/src/platform/main.py)
- **Webview 线程实现**：[`src/platform/webview.py`](file:///home/mickey/git/test_of_realtime_AI_agent/src/platform/webview.py)
- **airplane_simulator 仿真线程实现**：[`src/platform/airplane_simulator.py`](file:///home/mickey/git/test_of_realtime_AI_agent/src/platform/airplane_simulator.py)
- **服务类型**：多线程复合架构服务（主线程守护 + WebviewThread 异步网络服务 + airplane_simulator 运动推算线程）
- **默认监听端口**：`8000`（容器外部映射为 `8010`）
- **启动命令**（见 [`docker/supervisord.conf`](file:///home/mickey/git/test_of_realtime_AI_agent/docker/supervisord.conf)）：
  ```bash
  /app/.venv/bin/python main.py
  ```
- **核心定位**：作为整个系统的“数字孪生与仿真计算中枢平台”，包含两大内部线程：
  1. **`airplane_simulator` 线程**：负责空域实体对象管理、航迹推算、大圆航线解算、转向率与爬升率积分，并提供状态查询与修改（速度、航向、高度）的统一接口；
  2. **`WebviewThread` 线程**：作为表现层与通信网关，仅负责从 `airplane_simulator` 读取数据并向网页端及外部进程广播/返回。

---

## 2. 进程与线程架构模型

系统采用“主进程守护 + 专用业务子线程”的分离解耦架构设计：

```mermaid
flowchart TB
    subgraph PlatformProcess["Platform 进程 (main.py, PID: 主进程)"]
        subgraph MainThread["主线程 (Main Thread)"]
            Lifecycle["生命周期守护"]
            SignalHandler["信号监听器 (SIGINT, SIGTERM)\n优雅停机调度"]
        end

        subgraph SimulatorThread["airplane_simulator 线程 (可选启动/停止)"]
            SimLoop["物理积分循环: _run_loop() (约 0.5s/tick)"]
            MoveCalc["大圆航线与运动学算法: _move_object()"]
            SimState["实体状态与线程锁: _objects, _lock"]
            SimAPIs["对外接口: get_objects(), update_object(), set_speed()..."]
        end

        subgraph WebviewThread["Webview 子线程 (WebviewThread, daemon=True)"]
            UvicornServer["Uvicorn Web 服务器 (0.0.0.0:8000)"]
            SyncLoop["Webview 同步循环: sync_loop() (每 0.5 秒)"]
            
            subgraph Communications["通信与接口层"]
                StaticRouter["静态路由与模板渲染: /, /static"]
                RestAPIs["REST API 路由 (/api/*)"]
                WSEndpoint["WebSocket 服务端: /ws"]
            end
        end
    end

    MainThread -->|启动| WebviewThread
    SignalHandler -->|触发 stop()| SimulatorThread
    SignalHandler -->|触发 stop_webview()| UvicornServer

    RestAPIs -->|启停控制 /api/simulator/state| SimulatorThread
    RestAPIs -->|修改航速/航向/高度| SimAPIs
    RestAPIs -->|读取当前状态| SimAPIs

    SimLoop --> MoveCalc
    MoveCalc --> SimState
    SimAPIs <--> SimState

    SyncLoop -->|从仿真器拉取数据| SimAPIs
    SyncLoop -->|广播给前端| WSEndpoint
```

### 2.1 主线程职责
1. **进程初始化**：配置统一的根日志记录器（写入 [`logs/platform.log`](file:///home/mickey/git/test_of_realtime_AI_agent/logs/platform.log) 与标准输出）。
2. **线程派生**：通过 `threading.Thread(target=run_webview, name="WebviewThread", daemon=True)` 启动 Webview 业务线程。主线程启动时**不自动启动** `airplane_simulator`。
3. **健康监控**：主线程维持非阻塞等待，周期性检查 `webview_thread.is_alive()` 状态。
4. **平滑下线 (Graceful Shutdown)**：注册 `SIGINT` 和 `SIGTERM` 信号回调，捕获信号后调用 `airplane_sim.stop()` 和 `stop_webview()`，优雅终止子线程并退出。

### 2.2 airplane_simulator 线程职责
- **独立解算循环**：线程名固定为 `airplane_simulator`，由外部控制接口（REST API 或前端开关）按需启动与停止。
- **状态维护与互斥锁**：内置 `_lock = threading.Lock()`，保护 `_objects` 字典的线程安全。
- **对外接口**：
  - `start()` / `stop()` / `is_running()`：生命周期管理；
  - `get_objects()` / `get_motion_state()`：读取当前全部实体运动状态快照；
  - `add_object()` / `remove_object()`：增删实体；
  - `update_object()` / `set_speed()` / `set_heading()` / `set_altitude()`：修改实体运动参数。

### 2.3 Webview 线程职责
Webview 线程内运行 Uvicorn 服务器及 FastAPI 应用实例，承载：
- **纯展现层职责**：仅负责从 `airplane_simulator` 读取数据并推送到前端，不直接参与物理坐标推算。
- 静态页面及 Leaflet 地图渲染。
- 实时 WebSocket 长连接会话与广播。
- 转发外部进程（Controller、Pilot）的查询与修改请求至 `airplane_simulator`。

---

## 3. 物理推算引擎算法剖析

运动解算在 `airplane_simulator` 线程内按 $\Delta t \approx 0.5\text{s}$ 步长运行：

### 1) 水平大圆航线位移计算 (Great Circle Navigation)
地球半径常数定义：$R = 6,371,000\text{ m}$，航速换算常数：$1\text{ 节 (knot)} \approx 0.514444\text{ m/s}$。
- 位移距离：$d = \text{speed\_knots} \times 0.514444 \times \Delta t$
- 角距离：$\delta = \frac{d}{R}$
- 给定起始点纬度 $\varphi_1$、经度 $\lambda_1$ 以及航向角 $\theta$（弧度制）：
  $$\varphi_2 = \arcsin\left(\sin \varphi_1 \cos \delta + \cos \varphi_1 \sin \delta \cos \theta\right)$$
  $$\lambda_2 = \lambda_1 + \operatorname{atan2}\left(\sin \theta \sin \delta \cos \varphi_1, \cos \delta - \sin \varphi_1 \sin \varphi_2\right)$$

### 2) 高度与平飞/爬升/下降控制
- **目标高度逼近模式**：当存在 `target_altitude_ft` 时，飞行器将根据设定垂直速率（默认或未指定时为 400 ft/min）匀速爬升或下降：
  $$\Delta h_{\max} = \frac{\text{effective\_rate} \times \Delta t}{60}$$
  当高度差 $|\text{diff}| \le \Delta h_{\max}$ 时，高度直接收敛至目标高度；否则按方向步进 $\Delta h_{\max}$。
- **开环垂直速度模式**：若未设置目标高度，则按照 `vertical_speed_ft_min` 持续改变高度。

### 3) 航向角与转弯率平滑收敛 (Heading & Turn Rate)
- 角度归一化函数 `_normalize_heading()` 将所有航向映射到 $[0, 360)$ 范围。
- 当设定了目标航向 `target_heading` 时，计算最短转弯路径：
  $$\text{diff} = (\text{target\_heading} - \text{current\_heading} + 360) \pmod{360}$$
  $$\text{if } \text{diff} > 180 \implies \text{diff} = \text{diff} - 360 \quad (\text{负值代表左转})$$
- 允许的最大单步转角：$\Delta \theta_{\max} = \text{turn\_rate\_deg\_sec} \times \Delta t$（默认标准转弯率为 3.0°/s）。

---

## 4. API 接口与对外服务

### 4.1 REST API 清单

| 路径 | 方法 | 请求体 / Query 参数 | 响应结构 | 功能说明 | 调用方 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `/` | `GET` | 无 | HTML (动态植入 JS 缓存戳) | 访问地图控制台页面 | 浏览器 |
| `/api/simulator/state` | `GET` | 无 | `{"running": bool}` | 查询 airplane_simulator 线程运行状态 | 前端 UI, 外部脚本 |
| `/api/simulator/state` | `POST` | `{"running": bool}` | `{"status": "success", "running": bool}` | 启动或停止 airplane_simulator 线程 | 前端 UI, 外部脚本 |
| `/api/simulator/start` | `POST` | 无 | `{"status": "success", "running": true}` | 启动 airplane_simulator 线程 | 外部脚本 |
| `/api/simulator/stop` | `POST` | 无 | `{"status": "success", "running": false}` | 停止 airplane_simulator 线程 | 外部脚本 |
| `/api/airport` | `GET` | 无 | `{"lat": float, "lon": float, "runway_heading": float}` | 查询当前配置的机场与跑道参数 | Agent Controller |
| `/api/airport` | `POST` | `{"lat": float, "lon": float, "runway_heading": float}` | `{"status": "success", "airport": {...}}` | 更新机场位置与跑道朝向，并向 WS 广播 | 前端 UI |
| `/api/controller_state`| `GET` | 无 | `{"enabled": bool}` | 获取 AI 管制员是否启用状态 (启动默认 False) | Agent Controller, 切换脚本 |
| `/api/controller_state`| `POST` | `{"enabled": bool}` | `{"status": "success", "enabled": bool}` | 切换 AI 管制员启用状态，并向 WS 广播 | 前端 UI, 切换脚本 |
| `/api/objects` | `GET` | 无 | `List[ObjectDict]` | 从 airplane_simulator 读取所有物体实时数据 | Controller, Pilot |
| `/api/add` | `GET` | `name, lat, lon, speed, heading, altitude, ...` | `{"status": "success", "object": {...}}` | 通过 HTTP 向 simulator 新增飞行器 | 测试脚本 / 外部系统 |
| `/api/update` | `GET` | `id, speed, heading, altitude` | `{"status": "success", "object": {...}}` | 动态修改 simulator 中指定飞行器的航行参数 | Agent Pilot |
| `/api/radio_log` | `POST` | `{"source": str, "type": str, "text": str, "data": dict}` | `{"status": "success"}` | 接收无线电报文或控制参数，广播给前端显示 | Controller, Pilot |

### 4.2 WebSocket 服务 (`/ws`)
- **连接建立时**：主动推送全量飞行器状态（`type: "state"`）、机场数据（`type: "airport"`）、管制员状态（`type: "controller_state"`）以及模拟器状态（`type: "simulator_state"`）。
- **客户端消息解析**：处理前端发起的 `add`、`update`、`remove` 消息，转调 `airplane_simulator`。
- **服务端事件广播推送**：在 `airplane_simulator` 运行状态下，以 2 Hz 频率下推物理推算状态帧；并实时广播控制参数与无线电交互日志。

---

## 5. 异常处理与并发控制

1. **多线程互斥锁**：
   `airplane_simulator` 内部所有读写 `_objects` 字典的逻辑均由 `threading.Lock()` 严格互斥，杜绝多线程读写竞争。
2. **死连接自清理机制**：
   广播时捕获 `RuntimeError` 与 `WebSocketDisconnect` 异常，自动移出已断开的连接。
3. **线程级解耦与崩溃防御**：
   主线程持续感知 Webview 线程健康状态；`airplane_simulator` 运行期间捕获异常，保证仿真推算不影响 Webview 服务响应。
