# 机载飞行员智能体进程设计与实现文档 (process_agent_pilot)

## 1. 概述与定位

`AgentPilot` 是系统中的机载飞行员智能体执行核心。
在重构前，飞行员进程运行 Uvicorn HTTP 服务（8020 端口），对外暴露 `/radio_RX` 和 `/test_ai`，并使用 HTTP 客户端调用 Platform API。
在重构后：
1. **彻底废除飞行员自身的 HTTP 服务（8020 端口关闭）**。
2. **彻底废除向 Webview 与 Simulator 发起的 HTTP 请求**。
3. **彻底对象化为 `AgentPilot` 类**，并在独立的 OS 进程中运行。所有输入（指令接收）与输出（控制仿真器与日志回传）均通过高性能 `multiprocessing.Pipe` 管道实现。

---

## 2. 职责与功能

1. **自然语言无线电指令语义解析 (NLP via LLM)**：
   通过管道接收来自管制员或 Web 代理的自然语言指令文本（如：“请让 Drone-1 把航速提升至 250 节，左转航向 180，爬升到 10000 英尺”），加载 `src/agent_pilot/prompt_template.txt` 模板调用大模型，结构化提取控制参数：
   ```json
   {
       "target": "Drone-1",
       "speed": 250,
       "heading": 180,
       "altitude": 10000
   }
   ```
2. **目标飞行器智能解析与多机匹配 (Target Resolution)**：
   通过飞行数据专用管道向 Simulator 查询当前活动飞行器列表，将提取的 `target` 标识与飞机的 `name` 及 `id`（UUID）进行大小写不敏感匹配：
   - 支持根据呼号精确/多机同名匹配。
   - 支持根据 UUID 前缀或完整 UUID 匹配。
   - 支持同名机队统一调度（一指令同时控制多架同名飞机）。
3. **飞行器运动学目标下发**：
   解析成功后，通过飞行数据专用管道 (`pilot_sim_flight`) 直接调用飞行模拟器的 `update_object` 接口，原子化更新指定飞机的目标航速、目标航向或目标高度。
4. **机载参数日志记录与无线电纯净性规范**：
   大模型提取出飞行参数后，在本地记录标准业务日志（格式为 `Target: <name>, Speed: <spd>, Heading: <hdg>, Alt: <alt>`）。
   **核心纯净化设计**：提取出的飞行姿态参数属于机载控制数据，**不是无线电通信内容**，**严禁在无线电管道发送**；同时此处**不需要通过 IPC 发送给其他进程**，直接由标准 Logger 记录并通过系统通道汇聚至 `logs/master.log`，彻底杜绝解算参数污染 `logs/radio.log` 与 Webview 界面。
5. **管道 AI 连通性测试服务**：
   通过管道响应 Webview 代理发起的 `test_ai` 连通性测试请求，返回大模型连通性与往返延迟。

---

## 3. 面向对象设计

核心实现位于 `src/agent_pilot/pilot.py` 中的 `AgentPilot` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `conn_radio_simulator` | `Optional[Connection]` | 无线电专线：与 Simulator 连接的底层管道句柄 |
| `conn_flight_simulator`| `Optional[Connection]` | 飞行数据专线：与 Simulator 连接的底层管道句柄 |
| `conn_flight_webview`  | `Optional[Connection]` | 系统控制专线：与 Webview 连接的底层管道句柄 |
| `conn_flight_master`   | `Optional[Connection]` | 系统管理专线：与 Master 连接的底层管理管道句柄 |
| `ch_radio_sim` | `Optional[IPCChannel]` | 封装的 Simulator 无线电专线通信通道 (接收 `radio_RX`) |
| `ch_flight_sim`| `Optional[IPCChannel]` | 封装的 Simulator 飞行数据通信通道 (`get_objects`, `update_object`) |
| `ch_flight_webview` | `Optional[IPCChannel]` | 封装的 Webview 代理通信通道 (`test_ai`) |
| `ch_flight_master`  | `Optional[IPCChannel]` | 封装的 Master 系统管理与日志通信通道 |
| `prompt_file` | `Path` | 自然语言参数提取 Prompt 模板路径 |
| `_stop_event` | `asyncio.Event` | 协程停机事件信号 |

---

## 4. 指令执行时序与数据流

```
[模拟器进程 (无线电中继)] 
       |
       | [radio 队列] IPC call: radio_RX {"text": "Alpha 把航速提升至 300 节"}
       v
+-------------------------------------------------------------------------+
|                               AgentPilot                                |
|  1. prompt.format(input_text=text)                                      |
|  2. generate_ai_response() -> 提取 {"target": "Alpha", "speed": 300}   |
|  3. logger.info("Target: Alpha, Speed: 300, ...") (本地标准日志记录)   |
|     (不通过无线电专线发送，不通过 IPC 发送其他进程，不污染 radio.log)   |
|  4. [flight 队列] ch_flight_sim.call("get_objects")                     |
|     --> 收到空域列表并匹配到 UUID: ["uuid-001"]                         |
|  5. [flight 队列] ch_flight_sim.call("update_object",                   |
|         {"id": "uuid-001", "speed": 300})                              |
|     --> 模拟器物理引擎原子化更新飞行状态                                |
+-------------------------------------------------------------------------+
       |
       v
[radio 队列] 返回 radio_RX 响应结果:
{
    "status": "success",
    "resolved_count": 1,
    "resolved_ids": ["uuid-001"],
    "extracted": {"target": "Alpha", "speed": 300},
    "platform_responses": [...]
}
```

---

## 5. 通信接口规范

### 5.1 对内开放的管道接口 (Incoming IPC)

| Action 名称 | 请求方 | 接口类型 | 说明 | 返回值 |
| :--- | :--- | :--- | :--- | :--- |
| `radio_RX` | Simulator 进程 (中继), Webview 进程 | 请求/响应 | 接收自然语言无线电指令，调用大模型解算目标飞行器与控制参数，并驱动模拟器执行修改。 | `{"status": "success", "resolved_count": int, "resolved_ids": list, "extracted": dict, "platform_responses": list}` |
| `test_ai` | Webview 进程 | 请求/响应 | 测试飞行员端配置的大模型连通性与往返延迟。 | `{"status": "success", "source": "agent_pilot", "available": True, "latency": float, "response": str}` |
| `ping` | Master 进程 | 请求/响应 | 心跳存活检测。 | `{"status": "ok", "pid": int}` |
| `status` | Master 进程 | 请求/响应 | 状态巡检。 | `{"status": "ok", "pid": int, "running": bool}` |
| `stop` | Master 进程 | 单向通知 | 释放管道资源并平稳退出进程。 | 无 |

### 5.2 主动调用的管道接口 (Outgoing IPC)

| 目标进程 | Action 名称 | 调用方式 | 说明 |
| :--- | :--- | :--- | :--- |
| **Simulator 进程** | `get_objects` | `await ch_simulator.call(...)` | **维持物理读取机制**：获取当前所有飞行器列表，将呼号解析为目标飞机 UUID。 |
| **Simulator 进程** | `update_object` | `await ch_simulator.call(...)` | **维持物理写入机制**：下达运动学参数修改指令（更新目标航速、目标航向或目标高度）。 |
| **Simulator 进程** | `radio_log` | `ch_simulator.notify(...)` | 推送机载解析后的结构化无线电反馈，由模拟器同步转发至 Master (写入 radio.log) 与 Webview。 |
| **Webview 进程** | `add_radio_log` | `ch_webview.notify(...)` | 推送机载解析后的结构化控制参数，供前端雷达和监控台显示。 |
| **Master 进程** | `log` | `ch_master.notify(...)` (通过 `IPCLogHandler`) | 上报本进程标准日志数据，由 Master 统一汇聚写入 `logs/master.log` 并支持日志轮转。 |

---

## 6. 日志系统集成说明

本进程不直接创建本地日志文件（废弃原 `logs/pilot.log` 直写方式）：
1. 进程初始化时通过 `setup_child_logging(channel=self.ch_master, process_name="agent_pilot")` 配置 `IPCLogHandler`。
2. 进程内产生的全部日志记录异步通过 `ch_master` 发往 Master 进程统一记录与轮转。
3. 若管道断开或未接入 Master 管道，自动无缝回退到本地控制台输出，保障业务流程不挂起。
