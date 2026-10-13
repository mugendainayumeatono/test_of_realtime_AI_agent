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

1. **空域态势感知循环 (`run_loop`) 与地面飞机过滤**：
   以 10 秒为周期，通过管道向 Webview 进程查询当前管制使能开关、机场基准地理位置与跑道朝向、以及空域内所有飞行器的三维实时状态。
   **核心安全机制：严格过滤剔除处于地面状态 (`status == "ground"`) 的飞机**。若空域内所有飞机均处于地面状态，管制员立即停止本次调度并休眠，不调用大语言模型，也不产生无线电调度；仅为空中飞机 (`status != "ground"`) 组装决策上下文。
2. **大模型决策与冲突化解 (Prompt Engineering)**：
   若管制使能开启且存在**空中活动飞行器**，加载 `src/agent_controller/prompt_template.txt` 模板，注入机场与空中飞机动态列表，调用大模型（Gemini / OpenAI 兼容接口）推演进近排序或避碰指令。
3. **无线电管制指令下发**：
   - 当大模型生成需要改变飞行姿态的管制自然语言指令时，通过专用管道 (`ctrl_pilot`) 直接调用飞行员进程的 `radio_RX` 接口。
   - 同时通过管道 (`ctrl_webview`) 发送 `add_radio_log` 通知，将该指令推送到前端雷达界面展示。
   - 若模型研判某架飞机当前航迹正常无需调整，仅记录监控日志并推送至界面，不向飞行员下发调配指令。
4. **管道 AI 连通性测试服务**：
   通过管道响应 Webview 代理发起的 `test_ai` 连通性测试请求，返回大模型通道状态与调用延迟。

---

## 3. 面向对象设计

核心实现位于 `src/agent_controller/controller.py` 中的 `AgentController` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `conn_webview` | `Optional[Connection]` | 与 Webview 连接的底层管道句柄 |
| `conn_pilot` | `Optional[Connection]` | 与 Pilot 连接的底层管道句柄 |
| `conn_master` | `Optional[Connection]` | 与 Master 连接的底层管理管道句柄 |
| `ch_webview` | `Optional[IPCChannel]` | 封装的 Webview 管道通信通道 |
| `ch_pilot` | `Optional[IPCChannel]` | 封装的 Pilot 管道通信通道 |
| `ch_master` | `Optional[IPCChannel]` | 封装的 Master 管道通信通道 |
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
               [IPC: get_controller_state]
                                |
                       是否开启管制 (enabled)?
                      /                 \
                  否 /                   \ 是
                    v                     v
              [休眠 10 秒]      [IPC: get_airport]
                                [IPC: get_objects]
                                          |
                                 过滤空中飞机 (airborne):
                                 status != "ground"
                                          |
                                 是否有空中飞机且机场已设置?
                                /                 \
                            否 /                   \ 是
                              v                     v
                        [休眠 10 秒]     [仅将空中飞机组装至 Prompt 并调用大模型]
                                                    |
                                           解析大模型返回文本
                                          /         |         \
                             NO_COMMAND  /   单机名称\         \ 调配指令
                                        v           v           v
                                     [不操作]  [IPC: 仅推界面日志] [IPC: 下发指令至 Simulator radio_transmit]
                                                                        (由模拟器中继至 Pilot / Master / Webview)
```

---

## 5. 通信接口规范

### 5.1 对内开放的管道接口 (Incoming IPC)

| Action 名称 | 请求方 | 接口类型 | 说明 | 返回值 |
| :--- | :--- | :--- | :--- | :--- |
| `test_ai` | Webview 进程 | 请求/响应 | 测试管制员环境配置的大模型连通性与往返延迟。 | `{"status": "success", "source": "agent_controller", "available": True, "latency": float, "response": str}` |
| `ping` | Master 进程 | 请求/响应 | 心跳存活检测。 | `{"status": "ok", "pid": int}` |
| `status` | Master 进程 | 请求/响应 | 状态巡检。 | `{"status": "ok", "pid": int, "running": bool}` |
| `stop` | Master 进程 | 单向通知 | 停止监控循环并退出进程。 | 无 |

### 5.2 主动调用的管道接口 (Outgoing IPC)

| 目标进程 | Action 名称 | 调用方式 | 说明 |
| :--- | :--- | :--- | :--- |
| **Webview 进程** | `get_controller_state` | `await ch_webview.call(...)` | 获取 AI 管制员全局使能开关。 |
| **Webview 进程** | `get_airport` | `await ch_webview.call(...)` | 获取机场参考经纬度与跑道朝向。 |
| **Webview 进程** | `get_objects` | `await ch_webview.call(...)` | 获取当前空域全部飞机的空间姿态数据。 |
| **模拟器进程** | `radio_transmit` | `await ch_simulator.call(...)` | **中继发送无线电管制指令**。彻底解耦与 Pilot 的直接通信，指令统一由模拟器转发至 Pilot 并抄送 Master 和 Webview。 |
| **Master 进程** | `log` | `ch_master.notify(...)` (通过 `IPCLogHandler`) | 上报本进程标准日志数据，由 Master 统一汇聚写入 `logs/master.log` 并支持日志轮转。 |

---

## 6. 日志系统集成说明

本进程不直接创建本地日志文件（废弃原 `logs/controller.log` 直写方式）：
1. 进程初始化时通过 `setup_child_logging(channel=self.ch_master, process_name="agent_controller")` 配置 `IPCLogHandler`。
2. 进程内产生的全部日志记录异步通过 `ch_master` 发往 Master 进程统一记录与轮转。
3. 若管道断开或未接入 Master 管道，自动无缝回退到本地控制台输出，保障业务流程不挂起。
