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
   通过管道向 Simulator 查询当前活动飞行器列表，将提取的 `target` 标识与飞机的 `name` 及 `id`（UUID）进行大小写不敏感匹配：
   - 支持根据呼号精确/多机同名匹配。
   - 支持根据 UUID 前缀或完整 UUID 匹配。
   - 支持同名机队统一调度（一指令同时控制多架同名飞机）。
3. **飞行器运动学目标下发**：
   解析成功后，通过专用管道 (`pilot_sim`) 直接调用飞行模拟器的 `update_object` 接口，原子化更新指定飞机的目标航速、目标航向或目标高度。
4. **机载参数日志回传**：
   控制应用后，通过管道 (`pilot_webview`) 触发 `add_radio_log` 通知，将解算出的结构化参数回传给前端可视化界面，形成完整闭环。
5. **管道 AI 连通性测试服务**：
   通过管道响应 Webview 代理发起的 `test_ai` 连通性测试请求，返回大模型连通性与往返延迟。

---

## 3. 面向对象设计

核心实现位于 `src/agent_pilot/pilot.py` 中的 `AgentPilot` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `conn_webview` | `Optional[Connection]` | 与 Webview 连接的底层管道句柄 |
| `conn_simulator` | `Optional[Connection]` | 与 Simulator 连接的底层管道句柄 |
| `conn_controller` | `Optional[Connection]` | 与 Controller 连接的底层管道句柄 |
| `conn_master` | `Optional[Connection]` | 与 Master 连接的底层管理管道句柄 |
| `ch_webview` | `Optional[IPCChannel]` | 封装的 Webview 管道通信通道 |
| `ch_simulator` | `Optional[IPCChannel]` | 封装的 Simulator 管道通信通道 |
| `ch_controller` | `Optional[IPCChannel]` | 封装的 Controller 管道通信通道 |
| `ch_master` | `Optional[IPCChannel]` | 封装的 Master 管道通信通道 |
| `prompt_file` | `Path` | 自然语言参数提取 Prompt 模板路径 |
| `_stop_event` | `asyncio.Event` | 协程停机事件信号 |

---

## 4. 指令执行时序与数据流

```
[管制员 / Webview 网关] 
       |
       | IPC call: radio_RX {"text": "Alpha 把航速提升至 300 节"}
       v
+-----------------------------------------------------------+
|                        AgentPilot                         |
|  1. prompt.format(input_text=text)                        |
|  2. generate_ai_response() -> 提取 {"target": "Alpha", ...}|
|  3. ch_simulator.call("get_objects")                      |
|     --> 收到空域列表并匹配到 UUID: ["uuid-001"]           |
|  4. ch_simulator.call("update_object",                    |
|         {"id": "uuid-001", "speed": 300})                |
|  5. ch_webview.notify("add_radio_log",                    |
|         {"source": "agent_pilot", "data": {...}})         |
+-----------------------------------------------------------+
       |
       v
返回 radio_RX 响应结果:
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
| `radio_RX` | Webview 进程, Controller 进程 | 请求/响应 | 接收自然语言无线电指令，调用大模型解算目标飞行器与控制参数，并驱动模拟器执行修改。 | `{"status": "success", "resolved_count": int, "resolved_ids": list, "extracted": dict, "platform_responses": list}` |
| `test_ai` | Webview 进程 | 请求/响应 | 测试飞行员端配置的大模型连通性与往返延迟。 | `{"status": "success", "source": "agent_pilot", "available": True, "latency": float, "response": str}` |
| `ping` | Master 进程 | 请求/响应 | 心跳存活检测。 | `{"status": "ok", "pid": int}` |
| `status` | Master 进程 | 请求/响应 | 状态巡检。 | `{"status": "ok", "pid": int, "running": bool}` |
| `stop` | Master 进程 | 单向通知 | 释放管道资源并平稳退出进程。 | 无 |

### 5.2 主动调用的管道接口 (Outgoing IPC)

| 目标进程 | Action 名称 | 调用方式 | 说明 |
| :--- | :--- | :--- | :--- |
| **Simulator 进程** | `get_objects` | `await ch_simulator.call(...)` | 获取当前所有飞行器列表，用于将呼号/标识解析为具体飞机 UUID。 |
| **Simulator 进程** | `update_object` | `await ch_simulator.call(...)` | 下达运动学参数修改指令（更新目标航速、目标航向或目标高度）。 |
| **Webview 进程** | `add_radio_log` | `ch_webview.notify(...)` | 推送机载解析后的结构化控制参数，供前端雷达和监控台显示。 |
