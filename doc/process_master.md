# Master 管理进程设计与实现文档 (process_master)

## 1. 概述与定位

`MasterProcess` 是系统中的核心中枢守护进程，负责统管全系统的多进程拓扑生命周期。
在重构前，系统依赖外部第三方的 `supervisord` 守护多个进程；重构后，系统**完全使用 Python 标准库 `multiprocessing` 模块自主实现**，实现了轻量化、零外部进程依赖的高内聚架构。

---

## 2. 职责与功能

1. **IPC 管道拓扑初始化与分发**：
   在派生任何子进程前，预先创建所有子进程间所需的双向全双工管道对 (`multiprocessing.Pipe(duplex=True)`)，并将对应端点注入到子进程的启动参数中。
2. **多进程并发派生**：
   通过 `multiprocessing.Process` 分别拉起：
   - 飞行模拟器进程 (`airplane_simulator`)
   - Webview 网关服务进程 (`webview`)
   - 进近空管智能体进程 (`agent_controller`)
   - 机载飞行员智能体进程 (`agent_pilot`)
3. **守护进程化 (Daemonize)**：
   支持基于 Unix 双重 Fork 与会话脱离机制，启动 Master 后初始启动进程即可安全退出，Master 转入后台无缝运行。
4. **健康状态巡检与自动故障自愈 (Auto-Restart)**：
   主循环中每秒检测各子进程的 `is_alive()` 状态。若有子进程异常退出，Master 立即记录原因并自动重新拉起。
5. **系统级信号拦截与优雅关机**：
   统一捕获 `SIGINT` 与 `SIGTERM`，通过内部管理管道向各子进程下发平稳停止指令，回收进程资源，防止孤儿进程与僵尸进程的产生。

---

## 3. 面向对象设计

核心实现位于 `src/master.py` 中的 `MasterProcess` 类：

### 3.1 核心成员属性

| 属性名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| `host` | `str` | Webview 服务的监听主机地址（默认 `0.0.0.0`） |
| `port` | `int` | Webview 服务的监听端口（默认 `8000`） |
| `pid_file` | `Path` | Master 进程 PID 文件路径（`logs/master.pid`） |
| `auto_restart` | `bool` | 子进程崩溃时是否自动重启（默认 `True`） |
| `processes` | `Dict[str, Process]` | 维护的 4 大子进程字典映射 |
| `pipes` | `Dict[str, Tuple]` | 全系统所有双向全双工管道端点字典 |
| `master_channels`| `Dict[str, IPCChannel]`| Master 与各子进程连接的系统飞行/管理 IPC 通道 |
| `master_radio_channel`| `Optional[IPCChannel]`| Master 与 Simulator 连接的独立无线电专线 IPC 通道 |
| `_shutdown_event`| `multiprocessing.Event`| 系统停机信号量 |

### 3.2 双队列管道拓扑创建算法 (`_create_pipes`)

Master 在启动前建立以下两套物理隔离的独立管道对：

#### 1. 无线电通信专线队列 (Radio IPC Queue)
专用于管制员、模拟器中继、飞行员、Webview 与 Master 之间的自然语言无线电通话传输与广播：
```python
self.pipes["ctrl_sim_radio"] = multiprocessing.Pipe(duplex=True)     # 管制员 -> 模拟器中继
self.pipes["sim_pilot_radio"] = multiprocessing.Pipe(duplex=True)    # 模拟器中继 -> 飞行员
self.pipes["sim_webview_radio"] = multiprocessing.Pipe(duplex=True)  # 模拟器中继 -> Webview 广播
self.pipes["master_sim_radio"] = multiprocessing.Pipe(duplex=True)   # 模拟器中继 -> Master 专线日志 (radio.log)
```

#### 2. 飞行数据与系统控制队列 (Flight Data & System IPC Queue)
用于高频物理运动姿态更新、CRUD 实体操作、场景快照存取、健康探测与全系统常规日志上报：
```python
self.pipes["webview_sim_flight"] = multiprocessing.Pipe(duplex=True)   # Webview 与 Simulator
self.pipes["webview_ctrl_flight"] = multiprocessing.Pipe(duplex=True)  # Webview 与 Controller
self.pipes["webview_pilot_flight"] = multiprocessing.Pipe(duplex=True) # Webview 与 Pilot
self.pipes["pilot_sim_flight"] = multiprocessing.Pipe(duplex=True)     # Pilot 与 Simulator (姿态读写)

self.pipes["master_webview_flight"] = multiprocessing.Pipe(duplex=True)# Master 管理 Webview
self.pipes["master_sim_flight"] = multiprocessing.Pipe(duplex=True)    # Master 管理 Simulator
self.pipes["master_ctrl_flight"] = multiprocessing.Pipe(duplex=True)   # Master 管理 Controller
self.pipes["master_pilot_flight"] = multiprocessing.Pipe(duplex=True)  # Master 管理 Pilot
```
*(注：系统同时为向下兼容保留了不带 `_flight` 后缀的管道别名映射)*


---

## 4. 守护进程化实现原理

`daemonize_master()` 函数实现了标准 Unix 守护进程化标准规范：

```
[启动命令: python main.py start]
       |
  (初始父进程)
       |
     fork() --------> 父进程打印启动成功提示并 exit(0) [退出]
       |
  (一级子进程)
       |
   os.setsid() -----> 脱离原控制终端，建立新 Session
       |
     fork() --------> 一级子进程直接 exit(0) [退出]
       |
  (二级孙进程 - 最终 Master 守护进程)
       |
  重定向 stdin 至 /dev/null
  重定向 stdout/stderr 至 logs/master.log
       |
  写入 PID 至 logs/master.pid
       |
  执行 master.run() 启动子进程并进入常驻健康巡检
```

---

## 5. 暴露的控制与管理接口

### 5.1 命令行管理接口 (CLI)

通过根目录 `python main.py <action>` 或 `python src/main.py <action>` 提供：

| 命令 | 参数 | 功能说明 |
| :--- | :--- | :--- |
| `start` | `--host`, `--port` | **守护进程启动**。后台拉起 Master 进程，启动进程退出。 |
| `run` | `-f`, `--foreground` | **前台运行**。Master 保持在前台（不 fork），适用于 Docker 容器环境。 |
| `stop` | 无 | **停止系统**。读取 `logs/master.pid`，向 Master 发送 `SIGTERM` 优雅停机。 |
| `restart`| `-f` | **重启系统**。先执行 stop 后执行 start（或 run）。 |
| `status` | 无 | **状态查询**。查询 Master 是否运行，并列出受控子进程的 PID 及状态。 |

### 5.2 内部管理管道接口 (Master IPC)

Master 向各子进程开放并监听的管理接口：

| Action | 传输方向 | 说明 |
| :--- | :--- | :--- |
| `ping` | Master -> 子进程 (Req/Res) | 探测子进程心跳。子进程回复 `{"status": "ok", "pid": <pid>}`。 |
| `status` | Master -> 子进程 (Req/Res) | 查询子进程内部关键指标（如连接数、物体数量等）。 |
| `stop` | Master -> 子进程 (Notify) | 下发平稳停机指令，子进程收到后有序退出。 |
| `log` | 子进程 -> Master (Notify) | 子进程通过 IPC 异步上报 `LogRecord` 字典数据，由 Master 统一汇聚记录与轮转。 |
| `radio_log` | Simulator -> Master (Notify) | 模拟器中继上报的无线电通信记录，由 Master 统一记录至独立的 `logs/radio.log`。 |

---

## 6. 集中式日志系统与日志轮转 (Centralized Logging & Rotation)

### 6.1 架构设计
系统重构废除了各子进程私自初始化 `FileHandler` 分散写入各日志文件的方式，改为**由 Master 进程统管全系统日志汇聚与持久化**：
1. **子进程端 (`IPCLogHandler`)**：
   - 子进程（`airplane_simulator`, `webview`, `agent_controller`, `agent_pilot`）统一集成 `IPCLogHandler`。
   - 所有调用 `logger.info()`、`logger.error()` 等生成的标准 `logging.LogRecord` 记录，由 `record_to_dict` 转换为可跨进程序列化的字典（处理异常堆栈、时间戳、进程标识 `processName` 以及自定义 `extra` 属性等）。
   - Webview 服务的 Uvicorn 服务配置 `log_config=None`，使 Uvicorn 内部访问日志 (`uvicorn.access`) 和错误日志无缝向根日志器传播，统一通过 IPC 管道汇总。
   - 通过管理管道 `ch_master.notify("log", record_dict)` 异步发送至 Master 进程，无须阻塞等待响应，避免对核心业务循环产生性能抖动。
2. **Master 端集中持久化与轮转**：
   - Master 进程在 `master_channels` 上注册 `log` 动作处理器 `_handle_child_log`，对非字典或畸形负载进行健壮校验与容错，避免抛出异常。
   - 接收到日志消息后，通过 `dict_to_record` 还原为标准 `LogRecord`（自动填充缺省的字段与合法日志级别），并由 Master 的根日志器统一派发。
   - 统一输出格式包含进程标识：`%(asctime)s - [%(processName)s] - %(levelname)s - %(message)s`，清晰追溯日志源头进程（如 `[master]`, `[agent_controller]`, `[agent_pilot]`, `[airplane_simulator]`, `[webview]`）。
   - 集成 `RotatingFileHandler` 写入 `logs/master.log`，支持基于文件大小 (`maxBytes`) 和备份保留数量 (`backupCount`) 的自动日志轮转机制。

### 6.2 环境变量配置
系统支持通过以下环境变量对集中式日志与轮转进行动态调优：
- `LOG_MAX_BYTES`: 单个日志文件最大字节数，超出后触发轮转（默认 `10485760` 即 10MB）。
- `LOG_BACKUP_COUNT`: 保留的历史备份日志文件数量（默认 `5`，如 `master.log.1` 至 `master.log.5`）。
- `LOG_LEVEL`: 日志记录级别，支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（默认 `INFO`）。

### 6.3 本地容错与降级回退机制 (Fallback)
1. **管道未就绪/断开回退**：若子进程在独立单元测试模式下启动（`conn_master` 为 None），或 Master 进程异常退出导致 IPC 管道破损断开，`IPCLogHandler` 自动安全降级至本地 `StreamHandler`（控制台/标准输出），确保绝不阻塞或中断业务逻辑。
2. **防重入死锁保护**：`IPCLogHandler` 内置基于线程本地存储 (`threading.local`) 的重入标志，避免在管道通讯失败报错时触发递归日志风暴。

---

## 7. 独立无线电专用日志与自动轮转 (`logs/radio.log`)

为了满足航空无线电通话的独立审计和历史可追溯性需求，Master 进程开辟了独立的无线电日志体系：
1. **无线电专线监听 (`master_radio_channel`)**：Master 专门建立并通过独立的 `master_sim_radio` 管道挂载 `master_radio_channel`，仅在此通道上监听 `radio_log` 通知动作。飞行数据与系统控制队列 (`master_channels`) 绝不混杂挂载无线电动作。
2. **内容纯净性与防污染机制**：
   - 只有通过无线电专线队列传输的真实空管自然语言语音才会被写入 `logs/radio.log`。
   - 飞行员从大模型抽取飞行参数（如 `Target: ..., Speed: ...`）和管制员巡航状态监控不进入无线电专线，而是作为常规本地日志通过系统管道汇入通用的 `master.log`，绝不污染 `radio.log`。
3. **格式化输出**：接收到无线电报文后，提取 `source`、`type`、`text`、`timestamp` 属性，以 `[%(source)s] (%(type)s) %(text)s` 格式推入 `self.radio_logger`。
4. **独立文件轮转**：由独立的 `RotatingFileHandler`（默认最大 10MB，保留 5 份历史备份）持久化写入 `logs/radio.log`，完全与通用的 `master.log` 隔离，确保管制通话记录条理清晰。



