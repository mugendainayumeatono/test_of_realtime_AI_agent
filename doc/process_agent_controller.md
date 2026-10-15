# 进近空管智能体进程设计与实现文档 (process_agent_controller)

## 1. 概述与定位

`AgentController` 是系统中的空中交通管制员（ATC）决策大脑。
在重构前，管制员通过 HTTP 轮询 Platform 进程（8010 端口）获取数据，通过 HTTP POST 调用 Pilot 进程（8020 端口）下发指令，自身还暴露 8030 HTTP 端口供测试连通性。
在重构后：
1. **彻底废除管制员自身的 HTTP 服务（8030 端口关闭）**。
2. **彻底废除向 Webview 与 Pilot 发起的 HTTP 请求**。
3. **彻底对象化为 `AgentController` 类**，并在独立的 OS 进程中运行。所有内外部协同均通过高性能 `multiprocessing.Pipe` 管道实现。

---

## 2. 职责与功能

1. **空域态势感知循环 (`run_loop`)**：
   以 10 秒为周期，通过管道查询当前管制使能开关、机场基准地理位置与跑道朝向、以及通过 IPC 接口向模拟器查询空域内所有飞行器的实时状态。
2. **大模型决策与态势推演 (Prompt 拼接与结构化输入)**：
   若管制使能开启且存在飞行器数据，将通过 IPC 从模拟器获取的完整字典数组连同机场数据整体序列化为 JSON 字典，直接拼接到 `src/agent_controller/prompt_template.txt` 提示词的最后，不再进行逐个参数的散装插值。由大模型统一研判全部飞行器状态。
3. **结构化输出与 Pydantic 约束 (response_schema)**：
   利用 `response_schema` 与 Pydantic 的 `ScheduleItem` 规范大模型输出结构化 JSON 字典数组：
   - `id`: 飞机的呼号 / ID。
   - `isSchedule`: 是否需要调度。若模型研判飞机维持当前飞行状态即可（包括已在地面 `ground` 或已在正常进近航线上），填 `false`；需调整姿态则填 `true`。
   - `msg`: 大模型生成的自然语言无线电指令（`isSchedule=true` 时有效）。
4. **无线电管制指令拼接与下发**：
   - 遍历大模型返回的调度数组，对 `isSchedule=false` 的飞机仅在本地记录标准 `logger.info` 巡航监控日志，不污染无线电队列与日志。
   - 对 `isSchedule=true` 的所有调度指令进行汇总拼接（如 `", ".join(msgs)`），通过既存无线电通信逻辑（`send_radio_message`）统一广播下发。
5. **管道 AI 连通性测试服务**：
   通过飞行数据/系统管理管道响应 Webview 代理发起的 `test_ai` 连通性测试请求，返回大模型通道状态与调用延迟。

---

## 3. 面向对象设计

核心实现位于 `src/agent_controller/controller.py` 中的 `AgentController` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `conn_radio_simulator` | `Optional[Connection]` | 无线电专线：与 Simulator 连接的底层管道句柄 |
| `conn_flight_simulator`| `Optional[Connection]` | 飞行数据专线：与 Simulator 连接的底层管道句柄 |
| `conn_flight_webview`  | `Optional[Connection]` | 系统控制专线：与 Webview 连接的底层管道句柄 |
| `conn_flight_master`   | `Optional[Connection]` | 系统管理专线：与 Master 连接的底层管理管道句柄 |
| `ch_radio_sim` | `Optional[IPCChannel]` | 封装的 Simulator 无线电专线通信通道 (下发 `radio_transmit`) |
| `ch_flight_sim`| `Optional[IPCChannel]` | 封装的 Simulator 飞行数据通信通道 (`get_airport`, `get_objects`) |
| `ch_flight_webview` | `Optional[IPCChannel]` | 封装的 Webview 交互通道 (`get_controller_state`, `set_controller_state`, `test_ai`) |
| `ch_flight_master`  | `Optional[IPCChannel]` | 封装的 Master 系统管理与日志通信通道 |
| `enabled`           | `bool` | 管制员使能开关状态（进程初始化时自身初始化为 disabled 状态 `False`） |
| `prompt_file` | `Path` | 管制决策 Prompt 模板文件路径 |
| `poll_interval`| `float` | 空域监控轮询间隔（默认 10.0 秒） |
| `_stop_event` | `asyncio.Event` | 协程停机事件信号 |

---

## 4. 管制决策流程与状态机

```
                      +-------------------+
                      |      开始轮询     |
                      +---------+---------+
                                |
                                v
                   [检查自身状态: self.enabled]
                                |
                       是否开启管制 (enabled)?
                      /                 \
                  否 /                   \ 是
                    v                     v
              [休眠 10 秒]      [flight: get_airport]
                                [flight: get_objects] (完整字典数组)
                                          |
                                 是否有飞机且机场已设置?
                                /                 \
                            否 /                   \ 是
                              v                     v
                        [休眠 10 秒]     [将完整数据字典拼接到 Prompt 尾部]
                                                    |
                                    [调用 generate_ai_response(response_schema=List[ScheduleItem])]
                                                    |
                                            解析结构化输出数组
                                                    |
                                         遍历数组中各个飞机项:
                                     - isSchedule == False: 记录 monitoring only 日志
                                     - isSchedule == True: 收集 msg 指令
                                                    |
                                        是否有需要调度的指令?
                                       /                 \
                                   否 /                   \ 是
                                     v                     v
                                  [休眠]          [拼接全部指令 (", ".join)]
                                                           |
                                                [send_radio_message 下发无线电广播]
```

---

## 5. 通信接口规范

### 5.1 对内开放的管道接口 (Incoming IPC)

| Action 名称 | 请求方 | 传输队列 | 说明 | 返回值 |
| :--- | :--- | :--- | :--- | :--- |
| `get_controller_state` | Webview 进程 | 飞行/系统队列 | 获取 AI 管制员全局使能开关状态。 | `{"enabled": bool}` |
| `set_controller_state` | Webview 进程 | 飞行/系统队列 | 设置 AI 管制员全局使能开关状态。 | `{"status": "success", "enabled": bool}` |
| `test_ai` | Webview 进程 | 飞行/系统队列 | 测试管制员环境配置的大模型连通性与往返延迟。 | `{"status": "success", "source": "agent_controller", "available": True, "latency": float, "response": str}` |
| `ping` | Master 进程 | 飞行/系统队列 | 心跳存活检测。 | `{"status": "ok", "pid": int}` |
| `status` | Master 进程 | 飞行/系统队列 | 状态巡检。 | `{"status": "ok", "pid": int, "running": bool}` |
| `stop` | Master 进程 | 飞行/系统队列 | 停止监控循环并退出进程。 | 无 |

### 5.2 主动调用的管道接口 (Outgoing IPC)

| 目标进程 | Action 名称 | 传输队列 | 调用方式 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| **模拟器进程** | `get_airport` | 飞行数据队列 | `await ch_flight_sim.call(...)` | 获取机场参考经纬度与跑道朝向。 |
| **模拟器进程** | `get_objects` | 飞行数据队列 | `await ch_flight_sim.call(...)` | 获取当前空域全部飞机的空间姿态数据。 |
| **模拟器进程** | `radio_transmit` | 无线电通信专线 | `await ch_radio_sim.call(...)` | **中继发送无线电管制指令**。专线发送至模拟器，由模拟器转发至 Pilot 并抄送 Master 和 Webview。 |
| **Master 进程** | `log` | 系统管理队列 | `ch_flight_master.notify(...)` | 上报本进程标准日志数据，由 Master 统一汇聚写入 `logs/master.log` 并支持日志轮转。 |


---

## 6. 日志系统集成说明

本进程不直接创建本地日志文件（废弃原 `logs/controller.log` 直写方式）：
1. 进程初始化时通过 `setup_child_logging(channel=self.ch_master, process_name="agent_controller")` 配置 `IPCLogHandler`。
2. 进程内产生的全部日志记录异步通过 `ch_master` 发往 Master 进程统一记录与轮转。
3. 若管道断开或未接入 Master 管道，自动无缝回退到本地控制台输出，保障业务流程不挂起。
