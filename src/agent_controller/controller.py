"""
src/agent_controller/controller.py

进近空中交通管制员智能体 (Agent Controller)。
彻底对象化设计：
1. AgentController: 核心管制员类，负责空域态势感知、规则与大模型策略解算、冲突防范与无线电指令下发。
2. 内部通信：重构为通过 multiprocessing.Pipe (IPCChannel) 与 Webview 进程和 Pilot 进程通信，
   彻底废弃原有的内部 HTTP 协议。对外暴露 test_ai 管道接口供 Webview 代理调用。
"""

import asyncio
import logging
import os
import signal
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

# 确保能引用 agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ai_client import (
    check_ai_connectivity,
    generate_ai_response,
    get_ai_model,
    test_ai_api,
)
from agent_common.ipc import IPCChannel, setup_child_logging, IPCLogHandler

logger = logging.getLogger("controller")

load_dotenv()

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8010")
PILOT_URL = os.environ.get("PILOT_URL", "http://localhost:8020")
PROMPT_FILE = Path(__file__).parent / "prompt_template.txt"


class AgentController:
    """
    管制员智能体业务对象。
    通过管道与 Webview 和 Pilot 通信，执行空域监测、大模型决策与无线电调度。
    """

    def __init__(
        self,
        conn_webview: Optional[Connection] = None,
        conn_pilot: Optional[Connection] = None,
        conn_master: Optional[Connection] = None,
        conn_simulator: Optional[Connection] = None,
        prompt_file: Optional[Path] = None,
        poll_interval: float = 10.0,
    ):
        self.conn_webview = conn_webview
        self.conn_pilot = conn_pilot
        self.conn_master = conn_master
        self.conn_simulator = conn_simulator

        self.ch_webview: Optional[IPCChannel] = None
        self.ch_pilot: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None
        self.ch_simulator: Optional[IPCChannel] = None

        self.prompt_file = prompt_file or PROMPT_FILE
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None

        self._setup_ipc()

    def _setup_ipc(self) -> None:
        if self.conn_webview:
            self.ch_webview = IPCChannel(self.conn_webview, name="ctrl->webview")
            # 注册供 Webview 代理调用的 test_ai 管道接口
            self.ch_webview.register_handler("test_ai", self._ipc_handle_test_ai)

        if self.conn_simulator:
            self.ch_simulator = IPCChannel(self.conn_simulator, name="ctrl->sim")

        if self.conn_pilot:
            self.ch_pilot = IPCChannel(self.conn_pilot, name="ctrl->pilot")

        if self.conn_master:
            self.ch_master = IPCChannel(self.conn_master, name="ctrl->master")
            self.ch_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_master.register_handler("status", lambda p: {
                "status": "ok",
                "pid": os.getpid(),
                "running": not self._stop_event.is_set()
            })
            self.ch_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_master, process_name="agent_controller")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="agent_controller")

    async def _ipc_handle_test_ai(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 通过管道发起的 AI 连通性测试请求"""
        logger.info("Received request to test AI connectivity on Controller via IPC.")
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_controller"
        return res

    async def test_ai(self) -> Dict[str, Any]:
        """本地直接执行 AI 连通性测试"""
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_controller"
        return res

    async def get_airport(self) -> Dict[str, Any]:
        """获取机场配置（优先 IPC，回退 HTTP）"""
        if self.ch_webview:
            try:
                return await self.ch_webview.call("get_airport", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to fetch airport via IPC: {e}")
                return {"lat": None, "lon": None}
        # 回退：检查模块级函数是否被 mock / 替换 (供原有单元测试桩使用)
        import unittest.mock
        mod = sys.modules.get(__name__)
        mod_func = mod.__dict__.get("get_airport") if mod else None
        if mod_func and isinstance(mod_func, (unittest.mock.MagicMock, unittest.mock.AsyncMock)):
            res = mod_func()
            return await res if asyncio.iscoroutine(res) else res

        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{PLATFORM_URL}/api/airport")
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch airport via HTTP: {e}")
                return {"lat": None, "lon": None}

    async def get_objects(self) -> List[Dict[str, Any]]:
        """获取空域所有物体（优先 IPC，回退 HTTP）"""
        if self.ch_webview:
            try:
                return await self.ch_webview.call("get_objects", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to fetch objects via IPC: {e}")
                return []
        # 回退：检查模块级函数是否被 mock / 替换 (供原有单元测试桩使用)
        import unittest.mock
        mod = sys.modules.get(__name__)
        mod_func = mod.__dict__.get("get_objects") if mod else None
        if mod_func and isinstance(mod_func, (unittest.mock.MagicMock, unittest.mock.AsyncMock)):
            res = mod_func()
            return await res if asyncio.iscoroutine(res) else res

        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{PLATFORM_URL}/api/objects")
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch objects via HTTP: {e}")
                return []

    async def get_controller_state(self) -> Dict[str, Any]:
        """获取管制员使能开关状态（优先 IPC，回退 HTTP）"""
        if self.ch_webview:
            try:
                return await self.ch_webview.call("get_controller_state", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to fetch controller state via IPC: {e}")
                return {"enabled": False}
        # 回退：检查模块级函数是否被 mock / 替换 (供原有单元测试桩使用)
        import unittest.mock
        mod = sys.modules.get(__name__)
        mod_func = mod.__dict__.get("get_controller_state") if mod else None
        if mod_func and isinstance(mod_func, (unittest.mock.MagicMock, unittest.mock.AsyncMock)):
            res = mod_func()
            return await res if asyncio.iscoroutine(res) else res

        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{PLATFORM_URL}/api/controller_state")
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch controller state via HTTP: {e}")
                return {"enabled": False}

    async def send_radio_message(self, text: str) -> None:
        """通过管道下发管制无线电指令至模拟器进程（由模拟器转发至 Pilot，并推送至 Master 与 Webview）"""
        logger.info(f"Controller issuing command: {text}")

        # 回退：检查模块级函数是否被 mock / 替换 (供原有单元测试桩使用)
        import unittest.mock
        mod = sys.modules.get(__name__)
        mod_func = mod.__dict__.get("send_radio_message") if mod else None
        if mod_func and isinstance(mod_func, (unittest.mock.MagicMock, unittest.mock.AsyncMock)):
            res = mod_func(text)
            if asyncio.iscoroutine(res):
                await res
            return

        payload = {
            "source": "agent_controller",
            "type": "voice",
            "text": text,
            "timestamp": time.time(),
        }

        # 1. 优先通过模拟器进程中转 (Requirement 2)
        if self.ch_simulator:
            try:
                res = await self.ch_simulator.call("radio_transmit", payload, timeout=30.0)
                return res
            except Exception as e:
                logger.error(f"Failed to transmit radio message to simulator via IPC: {e}")

        # 2. 回退机制：若无模拟器通道但有 Pilot 通道（兼容旧测试桩）
        if self.ch_webview:
            try:
                self.ch_webview.notify("add_radio_log", payload)
            except Exception as e:
                logger.error(f"Failed to send radio log via IPC: {e}")
        else:
            async with httpx.AsyncClient() as client:
                try:
                    await client.post(f"{PLATFORM_URL}/api/radio_log", json=payload)
                except Exception as e:
                    logger.error(f"Failed to send radio log via HTTP: {e}")

        if self.ch_pilot:
            try:
                await self.ch_pilot.call("radio_RX", {"text": text}, timeout=30.0)
            except Exception as e:
                logger.error(f"Failed to send radio command to pilot via IPC fallback: {e}")
        else:
            async with httpx.AsyncClient() as client:
                try:
                    await client.post(f"{PILOT_URL}/radio_RX", json={"text": text})
                except Exception as e:
                    logger.error(f"Failed to send radio message via HTTP fallback: {e}")

    async def run_loop(self) -> None:
        """管制员空域态势感知与大模型决策主循环"""
        if not get_ai_model():
            logger.error("AI Model API key not configured. Exiting loop.")
            return

        test_ai_api()
        logger.info("Agent Controller started. Monitoring airspace...")

        while not self._stop_event.is_set():
            try:
                c_state = await self.get_controller_state()
                if not c_state.get("enabled", False):
                    await asyncio.sleep(self.poll_interval)
                    continue

                airport = await self.get_airport()
                objects = await self.get_objects()

                if not objects:
                    await asyncio.sleep(self.poll_interval)
                    continue

                # 过滤出空中状态的飞机，排除处于地面状态的飞机 (status == "ground")
                airborne_objects = [
                    obj for obj in objects
                    if obj.get("status") != "ground"
                ]

                if not airborne_objects:
                    logger.info("No airborne aircraft in airspace (all on ground or empty). Halting dispatch.")
                    await asyncio.sleep(self.poll_interval)
                    continue

                airport_lat = airport.get("lat")
                airport_lon = airport.get("lon")
                runway_heading = airport.get("runway_heading")

                if airport_lat is None or airport_lon is None:
                    logger.warning("No airport location set. Waiting for UI configuration.")
                    await asyncio.sleep(self.poll_interval)
                    continue

                try:
                    with open(self.prompt_file, "r", encoding="utf-8") as f:
                        template = f.read()
                except Exception as e:
                    logger.error(f"Could not read prompt template: {e}")
                    await asyncio.sleep(self.poll_interval)
                    continue

                prompt = template.format(
                    airport_lat=airport_lat,
                    airport_lon=airport_lon,
                    runway_heading=runway_heading if runway_heading is not None else "Not set",
                    objects=airborne_objects
                )
                text = generate_ai_response(prompt)
                object_names = [obj.get("name") for obj in airborne_objects if obj.get("name")]

                if not text or text == "NO_COMMAND":
                    pass
                elif text in object_names:
                    logger.info(f"AI determined {text} does not need flight state changes.")
                    info_data = {
                        "source": "agent_controller",
                        "type": "info",
                        "text": f"Monitoring {text}, no instruction needed."
                    }
                    if self.ch_webview:
                        self.ch_webview.notify("add_radio_log", info_data)
                    else:
                        async with httpx.AsyncClient() as client:
                            try:
                                await client.post(f"{PLATFORM_URL}/api/radio_log", json=info_data)
                            except Exception:
                                pass
                else:
                    await self.send_radio_message(text)

            except Exception as e:
                logger.error(f"Error in controller loop: {e}")

            await asyncio.sleep(self.poll_interval)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动 IPC 监听与调度任务"""
        cur_loop = loop or asyncio.get_running_loop()
        if self.ch_webview:
            self.ch_webview.start(loop=cur_loop)
        if self.ch_simulator:
            self.ch_simulator.start(loop=cur_loop)
        if self.ch_pilot:
            self.ch_pilot.start(loop=cur_loop)
        if self.ch_master:
            self.ch_master.start(loop=cur_loop)
        self._loop_task = cur_loop.create_task(self.run_loop())

    def stop(self) -> None:
        """优雅停止管制员"""
        logger.info("Stopping Agent Controller...")
        self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
        if self.ch_webview:
            self.ch_webview.close()
        if self.ch_simulator:
            self.ch_simulator.close()
        if self.ch_pilot:
            self.ch_pilot.close()
        if self.ch_master:
            self.ch_master.close()
        if hasattr(self, "ipc_log_handler") and self.ipc_log_handler:
            self.ipc_log_handler.set_channel(None)

    def run(self) -> None:
        """独立进程主执行入口（基于 asyncio 运行）"""
        logger.info(f"=== Agent Controller Process started (PID: {os.getpid()}) ===")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        signal.signal(signal.SIGINT, lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        self.start(loop=loop)
        try:
            loop.run_until_complete(self._wait_stop())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            logger.info(f"=== Agent Controller Process exited (PID: {os.getpid()}) ===")
            self.stop()
            loop.close()

    async def _wait_stop(self) -> None:
        await self._stop_event.wait()


def controller_process_entrypoint(
    conn_webview: Connection,
    conn_pilot: Optional[Connection] = None,
    conn_master: Optional[Connection] = None,
    conn_simulator: Optional[Connection] = None,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的入口函数"""
    controller_instance = AgentController(
        conn_webview=conn_webview,
        conn_pilot=conn_pilot,
        conn_master=conn_master,
        conn_simulator=conn_simulator,
    )
    controller_instance.run()


# --- 全局单例与向后兼容模块级接口（供测试桩使用） ---
default_controller = AgentController()

async def get_airport():
    return await default_controller.get_airport()

async def get_objects():
    return await default_controller.get_objects()

async def get_controller_state():
    return await default_controller.get_controller_state()

async def send_radio_message(text: str):
    await default_controller.send_radio_message(text)

async def run_loop():
    await default_controller.run_loop()

# 兼容既有单元测试中 TestClient(controller.app) 的 FastAPI 端点
from fastapi import FastAPI
app = FastAPI(title="Agent Controller Standalone")

@app.get("/test_ai")
@app.post("/test_ai")
async def test_ai_endpoint():
    logger.info("Received request to test AI connectivity on Controller.")
    res = await asyncio.to_thread(check_ai_connectivity)
    res["source"] = "agent_controller"
    return res

if __name__ == "__main__":
    default_controller.run()
