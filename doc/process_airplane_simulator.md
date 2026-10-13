# 飞行模拟器进程设计与实现文档 (process_airplane_simulator)

## 1. 概述与定位

`AirplaneSimulator` 是系统中的物理仿真推算核心。
在重构前，模拟器以单例对象运行在 Platform 进程的内部线程中；重构后，**飞行模拟器被彻底解耦为独立的 OS 进程 (`AirplaneSimulatorService`)**。
该进程专职负责大圆航线运动学计算、高度爬升/下降积分与航向转向积分，完全脱离 Webview 进程与网络 IO 的干扰，确保高确定性的仿真解算精度。

---

## 2. 职责与功能

1. **多飞行器实体状态维护**：
   维护空域中所有飞行器的瞬时位置（经纬度）、当前航向、当前高度、当前航速（节）、垂直升降速率（英尺/分钟）、转弯率（度/秒）以及目标高度与目标航向。
2. **大圆航线物理积分解算循环 (`_run_loop`)**：
   在启用状态下，以约为 0.5 秒为步长（`dt`），执行精准的球面大圆航线坐标积分、高度爬升率步进逼近以及最短角转弯率逼近。
3. **物体实体增删改查 (CRUD)**：
   提供线程安全的飞行器添加、运动学参数修改、删除以及快照查询。
4. **管道 IPC 通信服务**：
   通过 `AirplaneSimulatorService` 包装类，监听来自 Webview、Pilot 以及 Master 的管道请求，提供原子化、低延迟的解算与控制能力。

---

## 3. 面向对象设计

核心实现位于 `src/platform/airplane_simulator.py`：

```
+-----------------------------------------------------------+
|                AirplaneSimulatorService                   |
|  - 拥有 AirplaneSimulator 实例                             |
|  - 绑定 ch_webview, ch_pilot, ch_master (IPCChannel)       |
|  - 注册 IPC 接口，消息解包并调用底层算法                    |
+-----------------------------+-----------------------------+
                              |
                              v
+-----------------------------------------------------------+
|                     AirplaneSimulator                     |
|  - _objects: Dict[str, Dict] (实体字典)                    |
|  - _lock: threading.Lock (并发保护锁)                      |
|  - start() / stop() / is_running() (仿真控制)              |
|  - add_object() / update_object() / remove_object()       |
|  - _run_loop() -> _move_object() (大圆航线物理积分)        |
+-----------------------------------------------------------+
```

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `_objects` | `Dict[str, Dict]` | 存储空域所有飞机字典的内存哈希表，以 UUID 为主键 |
| `_lock` | `threading.Lock` | 保护 `_objects` 在高频物理更新与外部 IPC 查询下的线程安全锁 |
| `_thread` | `Optional[Thread]` | 执行 0.5s/tick 物理积分推算循环的内部工作线程 |
| `ch_flight_webview` | `Optional[IPCChannel]` | 飞行数据队列：与 Webview 连接，负责物体 CRUD、仿真启停与快照恢复 |
| `ch_flight_pilot` | `Optional[IPCChannel]` | 飞行数据队列：与 Pilot 连接，负责机载飞行参数读取与物理更新 |
| `ch_flight_master` | `Optional[IPCChannel]` | 系统管理队列：与 Master 连接，负责生命周期与系统运行日志上报 |
| `ch_radio_ctrl` | `Optional[IPCChannel]` | 无线电通信专线：接收管制员下发的 `radio_transmit` 指令 |
| `ch_radio_pilot` | `Optional[IPCChannel]` | 无线电通信专线：向飞行员转发 `radio_RX` 自然语言指令 |
| `ch_radio_webview` | `Optional[IPCChannel]` | 无线电通信专线：向 Webview 推流 `add_radio_log` 供前端与 CLI 显示 |
| `ch_radio_master` | `Optional[IPCChannel]` | 无线电通信专线：向 Master 推流 `radio_log` 写入独立 `radio.log` |


---

## 4. 运动学解算算法数学模型

模拟器在 `_move_object(obj, dt)` 中实现了三维空间物理运动学模型：

### 4.1 大圆航线地面轨迹积分 (Great Circle Kinematics)
设地球半径 $R = 6,371,000 \text{ m}$，航速换算系数 $1 \text{ knot} = 0.514444 \text{ m/s}$。
在时间差 $dt$ 内，飞机沿大地线滑跑距离 $d = v \cdot dt$。
计算角距离 $\delta = \frac{d}{R}$，根据大圆正弦/余弦球面三角学公式计算新坐标 $(\text{lat}_2, \text{lon}_2)$：
$$
\sin(\text{lat}_2) = \sin(\text{lat}_1)\cos(\delta) + \cos(\text{lat}_1)\sin(\delta)\cos(\theta)
$$
$$
\text{lon}_2 = \text{lon}_1 + \operatorname{atan2}\left(\sin(\theta)\sin(\delta)\cos(\text{lat}_1), \cos(\delta) - \sin(\text{lat}_1)\sin(\text{lat}_2)\right)
$$
其中 $\theta$ 为当前航向角（弧度）。

### 4.2 垂直爬升与下降模型
- 若设定了目标高度 $H_{\text{target}}$，且当前高度 $H \neq H_{\text{target}}$：
  - 默认垂直变化率 $V_z = 400 \text{ ft/min}$（或用户指定的 `vertical_speed_ft_min`）。
  - 最大步长高度增量 $\Delta H_{\max} = \frac{|V_z| \cdot dt}{60}$。
  - 若 $|H_{\text{target}} - H| \le \Delta H_{\max}$，则直接逼近到达 $H = H_{\text{target}}$；否则按差值符号步进爬升/下降。

### 4.3 航向转弯逼近模型
- 若设定了目标航向 $\psi_{\text{target}}$，计算最短转角差 $\Delta \psi = (\psi_{\text{target}} - \psi + 360) \pmod{360}$（若 $>180^\circ$ 则调整为逆时针转弯 $\Delta \psi - 360^\circ$）。
- 转弯角速度默认 $\omega = 3.0^\circ/\text{s}$。最大单步转向角 $\Delta \psi_{\max} = \omega \cdot dt$。
- 根据最短圆弧路径逼近目标航向，消除 $0^\circ \leftrightarrow 360^\circ$ 跳变。

### 4.4 飞行器空中/地面状态自动判定与转换模型 (Flight Status Transition)
- **状态字段**：每个飞行器实体维护 `status` 字段，枚举取值为 `"airborne"`（空中）与 `"ground"`（地面）。
- **判定阈值**：
  - 设标准海里换算常数 $1 \text{ NM} = 1852.0 \text{ m}$。
  - 通过半正矢大圆距离公式计算飞机位置 $(\text{lat}_1, \text{lon}_1)$ 与机场基准点 $(\text{lat}_{\text{apt}}, \text{lon}_{\text{apt}})$ 之间的地面距离 $d$。
  - **地面条件**：当距离 $d \le 1852.0 \text{ m}$（$\le 1\text{ NM}$）且飞机高度 $H < 100.0 \text{ ft}$ 时，状态判定为 `"ground"`。
  - **空中条件**：当脱离上述范围（$d > 1852.0 \text{ m}$ 或 $H \ge 100.0 \text{ ft}$）或未配置机场时，状态判定为 `"airborne"`。
- **状态转移制动动作**：
  - 当飞机状态**由空中变为地面**（`airborne -> ground`，即着陆接地瞬间），模拟器自动执行制动保护：
    1. 自动将飞机航速归零：`speed_knots = 0.0`；
    2. 自动将垂直升降速率归零：`vertical_speed_ft_min = 0.0`；
    3. 目标高度同步设为当前高度（`target_altitude_ft = altitude_ft`），使飞机稳态停泊在地面。

---

## 5. 对外开放的管道 IPC 接口规范

`AirplaneSimulatorService` 严格在双套独立管道上挂载对应的处理接口：

### 5.1 飞行数据与控制队列接口 (Flight Data IPC Queue)
挂载于 `ch_flight_webview` 与 `ch_flight_pilot`，严禁挂载无线电动作：

| Action 名称 | 请求参数 (Params) | 返回值说明 | 适用场景 |
| :--- | :--- | :--- | :--- |
| `get_objects` | 无 | `List[Dict]`，当前所有飞机的状态快照深度副本（包含 `status`）。 | Webview 广播推流，Pilot 目标匹配 |
| `get_object` | `{"id": str}` | `Optional[Dict]`，单个飞机对象的快照。 | 单机详情查询 |
| `get_motion_state`| 无 | `List[Dict]`，全量物体状态（别名）。 | 状态查询 |
| `add_object` | `name`, `lat`, `lon`, `speed`, `heading`, `altitude`, `target_heading`, `target_altitude`, `vertical_speed_ft_min`, `turn_rate_deg_sec` | `Dict`，包含分配的 UUID 与自动判定的 `status` 的完整新实体字典。 | Webview / CLI 创建飞机 |
| `update_object` | `id`, `speed`, `heading`, `altitude`, `speed_knots`, `altitude_ft`, `vertical_speed_ft_min`, `target_heading`, `target_altitude_ft`, `turn_rate_deg_sec`, `current_heading`, `name` | `{"found": bool, "object": Optional[Dict]}` | Pilot 飞机控制，Webview / CLI 修改参数 |
| `remove_object` | `{"id": str}` | `bool`，是否成功找到并删除。 | Webview / CLI 删除飞机 |
| `clear_objects` | 无 | `bool`，True。清空所有物体。 | 场景重置 |
| `restore_objects` | `{"objects": List[Dict]}` | `List[Dict]`，还原后的全量飞机状态列表。 | 场景快照还原（从 state.json 批量重建实体与运动学状态） |
| `set_airport` | `{"lat": float, "lon": float, "runway_heading": Optional[float]}` | `bool`，True。设置基准机场坐标并即时刷新全量飞机地面/空中状态。 | Webview 进程同步机场配置 |
| `get_airport` | 无 | `Optional[Dict]`，当前配置的机场地理坐标。 | 查询基准机场 |
| `start_simulator`| 无 | `{"status": "success", "running": True}` | 开启 0.5s/tick 物理积分推算循环 |
| `stop_simulator` | 无 | `{"status": "success", "running": False}` | 暂停物理积分推算循环 |
| `get_simulator_state`| 无 | `{"running": bool}` | 查询模拟器是否正在运行推算 |
| `set_simulator_state`| `{"running": bool}` | `{"status": "success", "running": bool}` | 切换模拟器运行状态 |
| `set_speed` | `{"id": str, "speed_knots": float}`| `bool`，是否设定成功。 | 快速设置航速 |
| `set_heading` | `{"id": str, "heading": float, "as_target": bool}` | `bool`，是否设定成功。 | 快速设置目标航向或当前航向 |
| `set_altitude` | `{"id": str, "altitude_ft": float, "as_target": bool}`| `bool`，是否设定成功。 | 快速设置目标高度或当前高度 |

### 5.2 无线电通信专线队列接口 (Radio IPC Queue)
仅挂载于 `ch_radio_ctrl`，确保无线电专线通信的纯净与低延迟：

| Action 名称 | 请求参数 (Params) | 返回值说明 | 适用场景 |
| :--- | :--- | :--- | :--- |
| `radio_transmit` | `{"text": str, "source": str, "type": str, "timestamp": float}` | `{"status": "success", "relayed": True, "pilot_response": Dict}` | 接收管制员语音指令，通过专线中继转发 Pilot，并推流 Master (`radio_log`) 与 Webview (`add_radio_log`) |

---

## 6. 无线电通信中继架构 (Radio Relay Hub)

在双队列无线电通信架构中，模拟器进程承担全系统无线电通信的中继中枢角色：
1. **专线接收与多方分发**：
   - 管制员通过无线电专线 `ctrl_sim_radio` 发送 `radio_transmit` 报文。
   - 模拟器收到后，通过无线电专线 `sim_pilot_radio` 调用飞行员的 `radio_RX` 接口下达语音指令。
   - 模拟器同步通过无线电专线 `master_sim_radio` 调用 `radio_log` 通知 Master 写入带轮转的 `logs/radio.log`。
   - 模拟器同步通过无线电专线 `sim_webview_radio` 调用 `add_radio_log` 通知 Webview 存入历史缓冲区并实时通过 WebSocket 广播。
2. **纯净性保障**：
   - 模拟器无线电专线严禁流转高频运动学积分数据或机载解算控制参数。
   - 飞行员从大模型抽取飞行参数后，仅在本地记录标准 logger，不通过无线电专线回传，彻底杜绝解算参数污染 `radio.log` 和 Webview 页面。
3. **飞行数据通道物理隔离**：飞行员机载智能体对飞机的查询 (`get_objects`) 与控制修改 (`update_object`) 依然严格走飞行数据专线 `pilot_sim_flight`，保持物理控制与无线电链路清晰分层。


---

## 7. 日志系统集成说明

本进程不直接创建本地独立日志文件：
1. 服务初始化时通过 `setup_child_logging(channel=self.ch_master, process_name="airplane_simulator")` 配置 `IPCLogHandler`。
2. 进程内产生的物理仿真与生命周期日志统一通过 `ch_master` 异步上报至 Master 进程汇聚写入 `logs/master.log` 并支持轮转。
3. 若无 Master 管道连接（如单体测试环境），自动安全回退至控制台标准输出，确保进程不会因日志通信阻塞。
