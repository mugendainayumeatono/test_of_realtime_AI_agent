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
| `ch_webview` | `Optional[IPCChannel]` | 与 Webview 进程连接的双向管道通道 |
| `ch_pilot` | `Optional[IPCChannel]` | 与 Pilot 进程连接的双向管道通道 |
| `ch_master` | `Optional[IPCChannel]` | 与 Master 进程连接的管理双向管道通道 |

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

---

## 5. 对外开放的管道 IPC 接口规范

`AirplaneSimulatorService` 对管道开放的处理接口：

| Action 名称 | 请求参数 (Params) | 返回值说明 | 适用场景 |
| :--- | :--- | :--- | :--- |
| `get_objects` | 无 | `List[Dict]`，当前所有飞机的状态快照深度副本。 | Webview 广播推流，Pilot 目标匹配 |
| `get_object` | `{"id": str}` | `Optional[Dict]`，单个飞机对象的快照。 | 单机详情查询 |
| `get_motion_state`| 无 | `List[Dict]`，全量物体状态（别名）。 | 状态查询 |
| `add_object` | `name`, `lat`, `lon`, `speed`, `heading`, `altitude`, `target_heading`, `target_altitude`, `vertical_speed_ft_min`, `turn_rate_deg_sec` | `Dict`，包含分配的 UUID 的完整新实体字典。 | Webview / CLI 创建飞机 |
| `update_object` | `id`, `speed`, `heading`, `altitude`, `speed_knots`, `altitude_ft`, `vertical_speed_ft_min`, `target_heading`, `target_altitude_ft`, `turn_rate_deg_sec`, `current_heading`, `name` | `{"found": bool, "object": Optional[Dict]}` | Pilot 飞机控制，Webview / CLI 修改参数 |
| `remove_object` | `{"id": str}` | `bool`，是否成功找到并删除。 | Webview / CLI 删除飞机 |
| `clear_objects` | 无 | `bool`，True。清空所有物体。 | 场景重置 |
| `start_simulator`| 无 | `{"status": "success", "running": True}` | 开启 0.5s/tick 物理积分推算循环 |
| `stop_simulator` | 无 | `{"status": "success", "running": False}` | 暂停物理积分推算循环 |
| `get_simulator_state`| 无 | `{"running": bool}` | 查询模拟器是否正在运行推算 |
| `set_simulator_state`| `{"running": bool}` | `{"status": "success", "running": bool}` | 切换模拟器运行状态 |
| `set_speed` | `{"id": str, "speed_knots": float}`| `bool`，是否设定成功。 | 快速设置航速 |
| `set_heading` | `{"id": str, "heading": float, "as_target": bool}` | `bool`，是否设定成功。 | 快速设置目标航向或当前航向 |
| `set_altitude` | `{"id": str, "altitude_ft": float, "as_target": bool}`| `bool`，是否设定成功。 | 快速设置目标高度或当前高度 |
