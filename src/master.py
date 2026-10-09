"""
src/master.py

系统主管理进程 (Master Process)。
彻底对象化设计：
1. MasterProcess: 管理系统所有子进程 (Webview, Airplane Simulator, Agent Controller, Agent Pilot) 的生命周期。
2. 进程间通信管道拓扑：由 Master 进程预先建立所有子进程间的 multiprocessing.Pipe 管道并分发，
   取代 supervisord，实现完全自研的多进程管理。
3. 守护进程化 (Daemonize): 支持启动后将 Master 转为后台守护进程，启动进程退出；亦支持前台运行模式（供 Docker 等容器环境使用）。
"""

import logging
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 设置项目根路径与导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "platform"))

from agent_common.ipc import IPCChannel
try:
    from airplane_simulator import simulator_process_entrypoint
    from webview import webview_process_entrypoint
except (ImportError, ModuleNotFoundError):
    from platform.airplane_simulator import simulator_process_entrypoint
    from platform.webview import webview_process_entrypoint

from agent_controller.controller import controller_process_entrypoint
from agent_pilot.pilot import pilot_process_entrypoint

log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("master")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "master.log"),
        logging.StreamHandler()
    ]
)

PID_FILE = log_dir / "master.pid"


class MasterProcess:
    """
    Master 进程管理核心对象。
    负责初始化所有 IPC 管道、启动并守护四大子业务进程、健康监控与优雅关机。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        pid_file: Path = PID_FILE,
        auto_restart: bool = True
    ):
        self.host = host
        self.port = port
        self.pid_file = pid_file
        self.auto_restart = auto_restart

        self.processes: Dict[str, multiprocessing.Process] = {}
        self.pipes: Dict[str, Tuple[Any, Any]] = {}
        self.master_channels: Dict[str, IPCChannel] = {}

        self._shutdown_event = multiprocessing.Event()

    def _create_pipes(self) -> None:
        """
        初始化全系统双向通信管道拓扑：
        1. webview <-> simulator (物体增删改查与物理仿真状态控制)
        2. webview <-> controller (Webview 代理请求 Controller AI 测试与状态查询)
        3. webview <-> pilot (Webview 代理下发 radio_RX 及 AI 测试)
        4. controller <-> pilot (Controller 无线电指令下发至 Pilot)
        5. pilot <-> simulator (Pilot 解析后更新飞机物理参数与查询目标)
        6. master <-> webview / simulator / controller / pilot (进程控制与心跳)
        """
        self.pipes["webview_sim"] = multiprocessing.Pipe(duplex=True)
        self.pipes["webview_ctrl"] = multiprocessing.Pipe(duplex=True)
        self.pipes["webview_pilot"] = multiprocessing.Pipe(duplex=True)
        self.pipes["ctrl_pilot"] = multiprocessing.Pipe(duplex=True)
        self.pipes["pilot_sim"] = multiprocessing.Pipe(duplex=True)

        self.pipes["master_webview"] = multiprocessing.Pipe(duplex=True)
        self.pipes["master_sim"] = multiprocessing.Pipe(duplex=True)
        self.pipes["master_ctrl"] = multiprocessing.Pipe(duplex=True)
        self.pipes["master_pilot"] = multiprocessing.Pipe(duplex=True)

        # Master 本地 IPC 通道
        self.master_channels["webview"] = IPCChannel(self.pipes["master_webview"][0], name="master->webview")
        self.master_channels["simulator"] = IPCChannel(self.pipes["master_sim"][0], name="master->sim")
        self.master_channels["controller"] = IPCChannel(self.pipes["master_ctrl"][0], name="master->ctrl")
        self.master_channels["pilot"] = IPCChannel(self.pipes["master_pilot"][0], name="master->pilot")

    def _start_child_processes(self) -> None:
        """实例化并启动 4 个独立的业务子进程"""
        logger.info("Initializing worker processes...")

        # 1. 飞行模拟器进程
        sim_proc = multiprocessing.Process(
            name="airplane_simulator",
            target=simulator_process_entrypoint,
            kwargs={
                "conn_webview": self.pipes["webview_sim"][1],
                "conn_pilot": self.pipes["pilot_sim"][1],
                "conn_master": self.pipes["master_sim"][1],
            }
        )

        # 2. Webview 进程 (对外 HTTP / WebSocket)
        webview_proc = multiprocessing.Process(
            name="webview",
            target=webview_process_entrypoint,
            kwargs={
                "conn_simulator": self.pipes["webview_sim"][0],
                "conn_controller": self.pipes["webview_ctrl"][0],
                "conn_pilot": self.pipes["webview_pilot"][0],
                "conn_master": self.pipes["master_webview"][1],
                "host": self.host,
                "port": self.port,
            }
        )

        # 3. 管制员智能体进程
        ctrl_proc = multiprocessing.Process(
            name="agent_controller",
            target=controller_process_entrypoint,
            kwargs={
                "conn_webview": self.pipes["webview_ctrl"][1],
                "conn_pilot": self.pipes["ctrl_pilot"][0],
                "conn_master": self.pipes["master_ctrl"][1],
            }
        )

        # 4. 飞行员智能体进程
        pilot_proc = multiprocessing.Process(
            name="agent_pilot",
            target=pilot_process_entrypoint,
            kwargs={
                "conn_webview": self.pipes["webview_pilot"][1],
                "conn_simulator": self.pipes["pilot_sim"][0],
                "conn_controller": self.pipes["ctrl_pilot"][1],
                "conn_master": self.pipes["master_pilot"][1],
            }
        )

        self.processes = {
            "airplane_simulator": sim_proc,
            "webview": webview_proc,
            "agent_controller": ctrl_proc,
            "agent_pilot": pilot_proc,
        }

        for name, proc in self.processes.items():
            proc.daemon = False
            proc.start()
            logger.info(f"Started child process [{name}] with PID: {proc.pid}")

        # 启动 Master 端各管理管道监听
        for ch in self.master_channels.values():
            ch.start()

    def _write_pid(self) -> None:
        """记录 Master 进程 PID"""
        try:
            self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write PID file {self.pid_file}: {e}")

    def _remove_pid(self) -> None:
        """清理 PID 文件"""
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
        except Exception:
            pass

    def stop(self) -> None:
        """优雅关闭所有子进程与 Master 进程"""
        if self._shutdown_event.is_set():
            return
        logger.info("=== Initiating Master Process Graceful Shutdown ===")
        self._shutdown_event.set()

        # 尝试通过 IPC 管道向各子进程下发 stop 命令
        for name, ch in self.master_channels.items():
            try:
                ch.notify("stop")
            except Exception:
                pass

        # 终止各子进程并等待回收
        for name, proc in self.processes.items():
            if proc.is_alive():
                logger.info(f"Waiting for process [{name}] (PID: {proc.pid}) to exit...")
                proc.join(timeout=3.0)
                if proc.is_alive():
                    logger.warning(f"Process [{name}] (PID: {proc.pid}) did not exit within timeout, terminating...")
                    proc.terminate()
                    proc.join(timeout=2.0)
                    if proc.is_alive():
                        proc.kill()

        # 关闭各管道通道
        for ch in self.master_channels.values():
            ch.close()

        self._remove_pid()
        logger.info("=== Master Process Shutdown Completed ===")

    def run(self) -> None:
        """主管理监控循环"""
        self._write_pid()
        logger.info(f"=== Master Process Started (PID: {os.getpid()}) ===")

        signal.signal(signal.SIGINT, lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        self._create_pipes()
        self._start_child_processes()

        try:
            while not self._shutdown_event.is_set():
                time.sleep(1.0)
                # 检查子进程健康状态
                for name, proc in list(self.processes.items()):
                    if not proc.is_alive() and not self._shutdown_event.is_set():
                        logger.error(f"Child process [{name}] (PID: {proc.pid}) died unexpectedly with code {proc.exitcode}!")
                        if self.auto_restart:
                            logger.info(f"Attempting to restart [{name}]...")
                            # 重新拉起对应进程
                            self._restart_process(name)
        except (KeyboardInterrupt, SystemExit):
            self.stop()
        finally:
            self.stop()

    def _restart_process(self, name: str) -> None:
        """重启意外崩溃的子进程"""
        if self._shutdown_event.is_set():
            return
        if name == "airplane_simulator":
            proc = multiprocessing.Process(
                name="airplane_simulator",
                target=simulator_process_entrypoint,
                kwargs={
                    "conn_webview": self.pipes["webview_sim"][1],
                    "conn_pilot": self.pipes["pilot_sim"][1],
                    "conn_master": self.pipes["master_sim"][1],
                }
            )
        elif name == "webview":
            proc = multiprocessing.Process(
                name="webview",
                target=webview_process_entrypoint,
                kwargs={
                    "conn_simulator": self.pipes["webview_sim"][0],
                    "conn_controller": self.pipes["webview_ctrl"][0],
                    "conn_pilot": self.pipes["webview_pilot"][0],
                    "conn_master": self.pipes["master_webview"][1],
                    "host": self.host,
                    "port": self.port,
                }
            )
        elif name == "agent_controller":
            proc = multiprocessing.Process(
                name="agent_controller",
                target=controller_process_entrypoint,
                kwargs={
                    "conn_webview": self.pipes["webview_ctrl"][1],
                    "conn_pilot": self.pipes["ctrl_pilot"][0],
                    "conn_master": self.pipes["master_ctrl"][1],
                }
            )
        elif name == "agent_pilot":
            proc = multiprocessing.Process(
                name="agent_pilot",
                target=pilot_process_entrypoint,
                kwargs={
                    "conn_webview": self.pipes["webview_pilot"][1],
                    "conn_simulator": self.pipes["pilot_sim"][0],
                    "conn_controller": self.pipes["ctrl_pilot"][1],
                    "conn_master": self.pipes["master_pilot"][1],
                }
            )
        else:
            return

        proc.daemon = False
        proc.start()
        self.processes[name] = proc
        logger.info(f"Successfully restarted [{name}] with new PID: {proc.pid}")


def get_master_pid() -> Optional[int]:
    """读取 PID 文件中的 Master 进程 PID"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # 检查该 pid 进程是否存在
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            return None
    return None


def daemonize_master(host: str = "0.0.0.0", port: int = 8000) -> None:
    """
    通过两次 fork 与 setsid 将 Master 进程转为后台守护进程。
    启动 master 进程的那个初始启动进程打印信息后即可退出。
    通过管道向启动进程回传真实的 Master 守护进程 PID。
    """
    existing_pid = get_master_pid()
    if existing_pid:
        print(f"[Notice] Master process is already running with PID: {existing_pid}")
        return

    # 创建管道以便孙进程向父进程回传真实的守护进程 PID
    r_fd, w_fd = os.pipe()

    # 第一次 fork
    pid = os.fork()
    if pid > 0:
        os.close(w_fd)
        try:
            real_pid_data = os.read(r_fd, 32).decode().strip()
            real_pid = int(real_pid_data) if real_pid_data else pid
        except Exception:
            real_pid = pid
        finally:
            os.close(r_fd)

        # 父进程（初始启动进程）打印准确的 Master PID 并退出
        print(f"[Success] Master process daemon launched in background (PID: {real_pid}).")
        print(f"Log output: {log_dir / 'master.log'}")
        sys.exit(0)

    # 子进程创建新会话，脱离控制终端
    os.close(r_fd)
    os.setsid()

    # 第二次 fork，防止守护进程重新获取控制终端
    pid2 = os.fork()
    if pid2 > 0:
        os.close(w_fd)
        sys.exit(0)

    # 孙进程（最终的 Master 守护进程）
    actual_pid = os.getpid()
    try:
        os.write(w_fd, str(actual_pid).encode())
    except Exception:
        pass
    finally:
        os.close(w_fd)

    # 重定向标准输入输出至日志文件与 /dev/null
    sys.stdout.flush()
    sys.stderr.flush()

    master_log = log_dir / "master.log"
    with open(master_log, "a", encoding="utf-8") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    master = MasterProcess(host=host, port=port)
    master.run()


def stop_master() -> bool:
    """停止正在运行的 Master 守护进程"""
    pid = get_master_pid()
    if not pid:
        print("[Notice] Master process is not running.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return False

    print(f"Stopping Master process (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            print("Master process did not exit in 6s, force killing...")
            os.kill(pid, signal.SIGKILL)
        if PID_FILE.exists():
            PID_FILE.unlink()
        print("[Success] Master process stopped.")
        return True
    except OSError as e:
        print(f"[Error] Failed to stop Master process: {e}")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return False


def status_master() -> None:
    """查询 Master 进程运行状态"""
    pid = get_master_pid()
    if pid:
        print(f"[Status] Master process is RUNNING (PID: {pid}).")
        # 列出子进程信息
        printed = False
        try:
            import subprocess
            res = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid,cmd"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout and len(res.stdout.strip().splitlines()) > 1:
                print("\nManaged child processes:")
                print(res.stdout.strip())
                printed = True
        except Exception:
            pass

        if not printed:
            # Fallback: 查询 /proc 目录获取子进程
            try:
                children = []
                proc_dir = Path("/proc")
                if proc_dir.exists():
                    for entry in proc_dir.iterdir():
                        if entry.is_dir() and entry.name.isdigit():
                            status_file = entry / "status"
                            if status_file.exists():
                                content = status_file.read_text(encoding="utf-8", errors="ignore")
                                for line in content.splitlines():
                                    if line.startswith("PPid:") and line.split()[1] == str(pid):
                                        cmd_file = entry / "cmdline"
                                        cmd = cmd_file.read_bytes().replace(b'\x00', b' ').decode(errors='replace').strip() if cmd_file.exists() else ""
                                        children.append((entry.name, cmd))
                if children:
                    print("\nManaged child processes:")
                    print("    PID CMD")
                    for c_pid, c_cmd in children:
                        print(f"  {c_pid:>5} {c_cmd}")
            except Exception:
                pass
    else:
        print("[Status] Master process is STOPPED.")
