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
| `master_channels`| `Dict[str, IPCChannel]`| Master 与各子进程连接的管理 IPC 通道 |
| `_shutdown_event`| `multiprocessing.Event`| 系统停机信号量 |

### 3.2 管道拓扑创建算法 (`_create_pipes`)

Master 在启动前建立以下 9 对管道：
```python
self.pipes["webview_sim"] = multiprocessing.Pipe(duplex=True)      # Webview 与 Simulator
self.pipes["webview_ctrl"] = multiprocessing.Pipe(duplex=True)     # Webview 与 Controller
self.pipes["webview_pilot"] = multiprocessing.Pipe(duplex=True)    # Webview 与 Pilot
self.pipes["ctrl_pilot"] = multiprocessing.Pipe(duplex=True)       # Controller 与 Pilot
self.pipes["pilot_sim"] = multiprocessing.Pipe(duplex=True)        # Pilot 与 Simulator

self.pipes["master_webview"] = multiprocessing.Pipe(duplex=True)   # Master 管理 Webview
self.pipes["master_sim"] = multiprocessing.Pipe(duplex=True)       # Master 管理 Simulator
self.pipes["master_ctrl"] = multiprocessing.Pipe(duplex=True)      # Master 管理 Controller
self.pipes["master_pilot"] = multiprocessing.Pipe(duplex=True)     # Master 管理 Pilot
```

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
