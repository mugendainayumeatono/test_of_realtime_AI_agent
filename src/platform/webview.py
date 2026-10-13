"""
src/platform/webview.py

Webview 界面服务与对外 API 网关。
彻底对象化设计：
1. WebviewApp: 封装 FastAPI 服务、WebSocket 连接池、状态持久化与状态广播循环。
2. 对外接口：严格保持供 CLI 和 Web 前端使用的所有 HTTP REST 与 WebSocket 接口原样不变。
3. 内部通信：重构为使用 multiprocessing.Pipe (IPCChannel) 与 Simulator、Controller、Pilot 进程交互，
   彻底替代原有的 HTTP 内部接口调用。
"""

import asyncio
import copy
import json
import logging
import os
import signal
import sys
import time
import uuid
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 确保路径能够导入 agent_common 与 airplane_simulator
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ipc import IPCChannel, setup_child_logging, IPCLogHandler
try:
    from airplane_simulator import (
        AirplaneSimulator,
        simulator as global_airplane_sim,
        _to_float,
        _normalize_heading,
    )
except (ImportError, ModuleNotFoundError):
    from .airplane_simulator import (
        AirplaneSimulator,
        simulator as global_airplane_sim,
        _to_float,
        _normalize_heading,
    )

logger = logging.getLogger("platform.webview")

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_FILE = Path(__file__).resolve().parent / "templates" / "index.html"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:8030")
PILOT_URL = os.environ.get("PILOT_URL", "http://127.0.0.1:8020")


class AirportLocation(BaseModel):
    lat: float
    lon: float
    runway_heading: Optional[float] = None


class ControllerState(BaseModel):
    enabled: bool


class SimulatorState(BaseModel):
    running: bool


class RadioLog(BaseModel):
    source: str
    type: str = "voice"
    data: Optional[Dict[str, Any]] = None
    text: Optional[str] = None
    timestamp: Optional[float] = None


class WebviewApp:
    """
    Webview 核心业务对象。
    对外提供 HTTP REST 与 WebSocket 接口，内部通过 IPC 管道协同其他进程。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        conn_simulator: Optional[Connection] = None,
        conn_controller: Optional[Connection] = None,
        conn_pilot: Optional[Connection] = None,
        conn_master: Optional[Connection] = None,
        local_simulator: Optional[AirplaneSimulator] = None,
        conn_radio_simulator: Optional[Connection] = None,
        conn_flight_simulator: Optional[Connection] = None,
        conn_flight_controller: Optional[Connection] = None,
        conn_flight_pilot: Optional[Connection] = None,
        conn_flight_master: Optional[Connection] = None,
    ):
        self.host = host
        self.port = port
        self.app = FastAPI(title="Webview Platform Gateway")
        self.clients: List[WebSocket] = []
        self.radio_logs: List[Dict[str, Any]] = []
        self.sync_task: Optional[asyncio.Task] = None
        self._server = None
        self._stop_event = asyncio.Event()

        # IPC 管道连接与通道
        self.conn_simulator = conn_simulator
        self.conn_controller = conn_controller
        self.conn_pilot = conn_pilot
        self.conn_master = conn_master

        # 显式双队列连接（未提供时平滑回退）
        self.conn_radio_sim = conn_radio_simulator or conn_simulator
        self.conn_flight_sim = conn_flight_simulator or conn_simulator
        self.conn_flight_ctrl = conn_flight_controller or conn_controller
        self.conn_flight_pilot = conn_flight_pilot or conn_pilot
        self.conn_flight_master = conn_flight_master or conn_master

        # 独立 IPC 通道
        self.ch_radio_sim: Optional[IPCChannel] = None
        self.ch_flight_sim: Optional[IPCChannel] = None
        self.ch_flight_ctrl: Optional[IPCChannel] = None
        self.ch_flight_pilot: Optional[IPCChannel] = None
        self.ch_flight_master: Optional[IPCChannel] = None

        # 兼容性通道别名
        self.ch_simulator: Optional[IPCChannel] = None
        self.ch_controller: Optional[IPCChannel] = None
        self.ch_pilot: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None

        # 本地模拟器（用于单进程/无管道单元测试环境回退）
        self.local_simulator = local_simulator or global_airplane_sim

        # 内存场景状态（与持久化 state.json 解耦）
        self.current_airport: Dict[str, Any] = {"lat": None, "lon": None, "runway_heading": None}
        self.controller_enabled: bool = False

        self._setup_ipc_channels()
        self._setup_routes()

    def _setup_ipc_channels(self) -> None:
        """初始化 IPC 通道并注册内部对外暴露的管道接口"""
        # 1. 飞行数据队列 (Flight Queue): 负责飞机状态、机场坐标与启停交互
        if self.conn_flight_sim:
            self.ch_flight_sim = IPCChannel(self.conn_flight_sim, name="webview->sim:flight")
            self._register_flight_handlers(self.ch_flight_sim)
            self.ch_simulator = self.ch_flight_sim

        # 2. 无线电通信队列 (Radio Queue): 专线接收来自模拟器的纯净无线电日志
        if self.conn_radio_sim:
            if self.conn_radio_sim is self.conn_flight_sim and self.ch_flight_sim:
                self.ch_radio_sim = self.ch_flight_sim
                self.ch_radio_sim.register_handler("add_radio_log", self._ipc_handle_radio_log)
                self.ch_radio_sim.register_handler("radio_log", self._ipc_handle_radio_log)
            else:
                self.ch_radio_sim = IPCChannel(self.conn_radio_sim, name="webview<-sim:radio")
                self.ch_radio_sim.register_handler("add_radio_log", self._ipc_handle_radio_log)
                self.ch_radio_sim.register_handler("radio_log", self._ipc_handle_radio_log)

        # 3. 飞行/系统控制队列 (Flight/System Queue): Master 系统监控与日志
        if self.conn_flight_master:
            self.ch_flight_master = IPCChannel(self.conn_flight_master, name="webview->master:flight")
            self.ch_master = self.ch_flight_master
            self.ch_flight_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_flight_master.register_handler("status", lambda p: {
                "status": "ok",
                "pid": os.getpid(),
                "clients_count": len(self.clients)
            })
            self.ch_flight_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_flight_master, process_name="webview")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="webview")

        # 4. Controller 与 Pilot 辅助控制通道
        if self.conn_flight_ctrl:
            self.ch_flight_ctrl = IPCChannel(self.conn_flight_ctrl, name="webview->ctrl:flight")
            self._register_flight_handlers(self.ch_flight_ctrl)
            self.ch_controller = self.ch_flight_ctrl

        if self.conn_flight_pilot:
            self.ch_flight_pilot = IPCChannel(self.conn_flight_pilot, name="webview->pilot:flight")
            self._register_flight_handlers(self.ch_flight_pilot)
            self.ch_pilot = self.ch_flight_pilot

    def _register_flight_handlers(self, channel: IPCChannel) -> None:
        """向飞行数据通道注册内部查询接口（严禁挂载无线电日志处理器）"""
        channel.register_handler("get_airport", lambda p: copy.deepcopy(self.current_airport))
        channel.register_handler("get_controller_state", lambda p: {"enabled": self.controller_enabled})
        channel.register_handler("get_objects", self._ipc_handle_get_objects)

    def _register_internal_handlers(self, channel: IPCChannel) -> None:
        """兼容旧测试桩：注册全量处理器"""
        self._register_flight_handlers(channel)
        channel.register_handler("add_radio_log", self._ipc_handle_radio_log)
        channel.register_handler("radio_log", self._ipc_handle_radio_log)

    async def _ipc_handle_get_objects(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """内部异步获取物体列表（不阻断 Webview 事件循环）"""
        return await self.get_objects_list()

    def _ipc_get_objects_sync(self) -> List[Dict[str, Any]]:
        """内部同步获取物体列表（非事件循环环境调用备用）"""
        if self.ch_simulator:
            return self.ch_simulator.call_sync("get_objects", {}, timeout=5.0)
        return self.local_simulator.get_objects()

    async def _ipc_handle_radio_log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """处理通过管道传入的无线电日志并广播至 Web 前端与暂存"""
        source = params.get("source", "unknown")
        msg_type = params.get("type", "voice")
        data = params.get("data")
        text = params.get("text")
        ts = params.get("timestamp") or time.time()
        logger.info(f"IPC radio log from {source}: type={msg_type}, text={text}")
        payload = {
            "type": "radio_log",
            "source": source,
            "msg_type": msg_type,
            "data": data,
            "text": text,
            "timestamp": ts,
        }
        self.radio_logs.append(payload)
        if len(self.radio_logs) > 1000:
            self.radio_logs.pop(0)
        await self._broadcast_payload(payload)
        return {"status": "success"}

    def get_persisted_state(self) -> Dict[str, Any]:
        """读取持久化状态文件"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to read state file {STATE_FILE}: {e}")
        return {"airport": {"lat": None, "lon": None, "runway_heading": None}, "objects": [], "controller_enabled": False}

    def save_persisted_state(self, state: Dict[str, Any]) -> None:
        """保存持久化状态文件"""
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save state to {STATE_FILE}: {e}")

    # --- 模拟器 IPC 辅助封装 ---
    async def get_objects_list(self) -> List[Dict[str, Any]]:
        """获取当前物体列表（优先 IPC，回退本地单例）"""
        if self.ch_simulator:
            try:
                return await self.ch_simulator.call("get_objects", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to query objects via simulator IPC: {e}")
                return []
        return self.local_simulator.get_objects()

    async def is_simulator_running(self) -> bool:
        """获取模拟器运行状态（优先 IPC，回退本地单例）"""
        if self.ch_simulator:
            try:
                res = await self.ch_simulator.call("get_simulator_state", timeout=5.0)
                return bool(res.get("running", False))
            except Exception as e:
                logger.error(f"Failed to query simulator state via IPC: {e}")
                return False
        return self.local_simulator.is_running()

    async def broadcast_state(self) -> None:
        """向所有 WebSocket 客户端广播物体最新位置快照"""
        if not self.clients:
            return
        current_objs = await self.get_objects_list()
        payload = {"type": "state", "objects": current_objs}
        await self._broadcast_payload(payload)

    async def _broadcast_payload(self, payload: Dict[str, Any]) -> None:
        """底层广播至所有已连接客户端"""
        disconnected: List[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(ws)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self.clients:
                self.clients.remove(ws)

    async def sync_loop(self) -> None:
        """后台状态同步循环 (约 0.5s/tick)"""
        while True:
            await asyncio.sleep(0.5)
            if self.clients and (await self.is_simulator_running()):
                await self.broadcast_state()

    def _setup_routes(self) -> None:
        """注册所有路由和 WebSocket 端点"""
        app = self.app
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.on_event("startup")
        async def on_startup():
            logger.info("Webview service starting up.")
            loop = asyncio.get_running_loop()
            if self.ch_simulator:
                self.ch_simulator.start(loop=loop)
            if self.ch_controller:
                self.ch_controller.start(loop=loop)
            if self.ch_pilot:
                self.ch_pilot.start(loop=loop)
            if self.ch_master:
                self.ch_master.start(loop=loop)

            # 进程启动时，controller_enabled 初始化为 False（不自动读取 state.json 设定状态）
            self.controller_enabled = False
            logger.info("Initialized controller_state to enabled=False on startup (initial clean airspace).")

            if self.sync_task is None:
                self.sync_task = asyncio.create_task(self.sync_loop())

        @app.on_event("shutdown")
        async def on_shutdown():
            logger.info("Webview service shutting down.")
            if self.sync_task:
                self.sync_task.cancel()
            if self.ch_simulator:
                self.ch_simulator.close()
            if self.ch_controller:
                self.ch_controller.close()
            if self.ch_pilot:
                self.ch_pilot.close()
            if self.ch_master:
                self.ch_master.close()
            if hasattr(self, "ipc_log_handler") and self.ipc_log_handler:
                self.ipc_log_handler.set_channel(None)

        @app.get("/", response_class=HTMLResponse)
        async def get_root(request: Request) -> HTMLResponse:
            html = TEMPLATE_FILE.read_text(encoding="utf-8")
            app_js_path = STATIC_DIR / "app.js"
            js_mtime = int(app_js_path.stat().st_mtime) if app_js_path.exists() else int(time.time())
            html = html.replace("{{version}}", str(js_mtime))
            response = HTMLResponse(html)
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        # --- 机场配置接口 ---
        @app.get("/api/airport")
        async def get_airport():
            return copy.deepcopy(self.current_airport)

        @app.post("/api/airport")
        async def set_airport(loc: AirportLocation):
            logger.info(f"Setting airport location to lat={loc.lat}, lon={loc.lon}, runway_heading={loc.runway_heading}")
            data = {"lat": loc.lat, "lon": loc.lon, "runway_heading": loc.runway_heading}
            self.current_airport = data

            if self.ch_simulator:
                try:
                    await self.ch_simulator.call("set_airport", data)
                except Exception as e:
                    logger.error(f"Failed to sync airport to simulator via IPC: {e}")
            else:
                self.local_simulator.set_airport(
                    lat=loc.lat,
                    lon=loc.lon,
                    runway_heading=loc.runway_heading,
                )

            payload = {"type": "airport", "lat": loc.lat, "lon": loc.lon, "runway_heading": loc.runway_heading}
            await self._broadcast_payload(payload)
            return {"status": "success", "airport": data}

        # --- AI 管制员状态接口 ---
        @app.get("/api/controller_state")
        async def get_controller_state():
            return {"enabled": self.controller_enabled}

        @app.post("/api/controller_state")
        async def set_controller_state(c_state: ControllerState):
            logger.info(f"Setting controller state to enabled={c_state.enabled}")
            self.controller_enabled = c_state.enabled

            payload = {"type": "controller_state", "enabled": c_state.enabled}
            await self._broadcast_payload(payload)
            return {"status": "success", "enabled": c_state.enabled}

        # --- 场景状态持久化与快照管理接口 ---
        @app.get("/api/state")
        async def get_state():
            """获取当前场景内存快照（机场、飞机列表、控制器状态）"""
            current_objs = await self.get_objects_list()
            return {
                "status": "success",
                "airport": copy.deepcopy(self.current_airport),
                "objects_count": len(current_objs),
                "objects": current_objs,
                "controller_enabled": self.controller_enabled,
            }

        @app.post("/api/state/save")
        async def save_state():
            """将当前所有机场坐标、飞机坐标、全部飞行状态保存到 state.json"""
            airport = copy.deepcopy(self.current_airport)
            if airport.get("lat") is None:
                if self.ch_simulator:
                    try:
                        sim_airport = await self.ch_simulator.call("get_airport", timeout=5.0)
                        if sim_airport:
                            airport = sim_airport
                    except Exception:
                        pass
                elif self.local_simulator:
                    sim_airport = self.local_simulator.get_airport()
                    if sim_airport:
                        airport = sim_airport

            objects = await self.get_objects_list()
            state_data = {
                "airport": airport,
                "objects": objects,
                "controller_enabled": self.controller_enabled,
            }
            self.save_persisted_state(state_data)
            logger.info(f"Scene state saved to {STATE_FILE}: {len(objects)} objects, airport={airport}")
            return {
                "status": "success",
                "message": "Scene state saved successfully to state.json",
                "airport": airport,
                "objects_count": len(objects),
                "objects": objects,
            }

        @app.post("/api/state/load")
        @app.post("/api/state/restore")
        async def load_state():
            """从 state.json 读取数据，还原机场坐标与全部飞机飞行状态"""
            if not STATE_FILE.exists():
                return {
                    "status": "error",
                    "message": f"State file {STATE_FILE.name} does not exist",
                }
            try:
                state_data = self.get_persisted_state()
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to read state file: {e}",
                }

            # 1. 还原机场
            airport_data = state_data.get("airport", {})
            if airport_data and airport_data.get("lat") is not None and airport_data.get("lon") is not None:
                self.current_airport = {
                    "lat": _to_float(airport_data.get("lat")),
                    "lon": _to_float(airport_data.get("lon")),
                    "runway_heading": _to_float(airport_data.get("runway_heading")) if airport_data.get("runway_heading") is not None else None,
                }
                if self.ch_simulator:
                    try:
                        await self.ch_simulator.call("set_airport", self.current_airport)
                    except Exception as e:
                        logger.error(f"Failed to restore airport to simulator via IPC: {e}")
                else:
                    self.local_simulator.set_airport(
                        lat=self.current_airport.get("lat"),
                        lon=self.current_airport.get("lon"),
                        runway_heading=self.current_airport.get("runway_heading"),
                    )
                await self._broadcast_payload({
                    "type": "airport",
                    "lat": self.current_airport.get("lat"),
                    "lon": self.current_airport.get("lon"),
                    "runway_heading": self.current_airport.get("runway_heading"),
                })
            else:
                self.current_airport = {"lat": None, "lon": None, "runway_heading": None}
                if self.ch_simulator:
                    try:
                        await self.ch_simulator.call("set_airport", {"lat": None, "lon": None, "runway_heading": None})
                    except Exception:
                        pass
                else:
                    self.local_simulator.set_airport(None, None, None)
                await self._broadcast_payload({
                    "type": "airport",
                    "lat": None,
                    "lon": None,
                    "runway_heading": None,
                })

            # 2. 还原飞机实体及全部飞行状态参数
            objects_data = state_data.get("objects", [])
            restored_objects = []
            if self.ch_simulator:
                try:
                    restored_objects = await self.ch_simulator.call("restore_objects", {"objects": objects_data})
                except Exception as e:
                    logger.error(f"Failed to restore objects to simulator via IPC: {e}")
            else:
                restored_objects = self.local_simulator.restore_objects(objects_data)

            # 广播最新状态至前端
            await self.broadcast_state()

            logger.info(f"Scene state restored from {STATE_FILE}: {len(restored_objects)} objects restored, airport={self.current_airport}")
            return {
                "status": "success",
                "message": "Scene state restored successfully from state.json",
                "airport": copy.deepcopy(self.current_airport),
                "objects_count": len(restored_objects),
                "objects": restored_objects,
            }

        # --- 场景重置接口 (清空飞机与机场，停止模拟器与AI管制员) ---
        @app.post("/api/reset")
        async def reset_airspace():
            """
            重置空域场景：
            1. 通知模拟器侧删除全部飞机与机场
            2. 停止飞行模拟器
            3. 停止 AI 管制员
            4. 向所有已连接 WebSocket 客户端广播状态清理
            """
            logger.info("Resetting airspace: clearing aircraft and airport, stopping simulator and controller.")

            # 1. 模拟器：删除全部飞机、清除机场、停止仿真推算循环
            if self.ch_simulator:
                try:
                    await self.ch_simulator.call("clear_objects", {}, timeout=5.0)
                except Exception as e:
                    logger.error(f"Failed to clear objects via simulator IPC: {e}")
                try:
                    await self.ch_simulator.call("set_airport", {"lat": None, "lon": None, "runway_heading": None}, timeout=5.0)
                except Exception as e:
                    logger.error(f"Failed to reset airport via simulator IPC: {e}")
                try:
                    await self.ch_simulator.call("set_simulator_state", {"running": False}, timeout=5.0)
                except Exception as e:
                    logger.error(f"Failed to stop simulator via IPC: {e}")
            else:
                self.local_simulator.clear_objects()
                self.local_simulator.set_airport(None, None, None)
                self.local_simulator.stop()

            # 2. 内存状态与控制器：停止 AI 管制员，清空内存机场坐标
            self.current_airport = {"lat": None, "lon": None, "runway_heading": None}
            self.controller_enabled = False

            # 3. WebSocket 广播最新清空与停止状态
            await self._broadcast_payload({"type": "airport", "lat": None, "lon": None, "runway_heading": None})
            await self._broadcast_payload({"type": "controller_state", "enabled": False})
            await self._broadcast_payload({"type": "simulator_state", "running": False})
            await self.broadcast_state()

            return {
                "status": "success",
                "message": "Airspace reset successfully: cleared aircraft and airport, stopped simulator and controller",
                "airport": self.current_airport,
                "objects_count": 0,
                "objects": [],
                "simulator_running": False,
                "controller_enabled": False,
            }

        # --- 仿真器状态控制接口 ---
        @app.get("/api/simulator/state")
        async def get_sim_state():
            running = await self.is_simulator_running()
            return {"running": running}

        @app.post("/api/simulator/state")
        async def set_sim_state(sim_state: SimulatorState):
            logger.info(f"Setting airplane_simulator state to running={sim_state.running}")
            if self.ch_simulator:
                try:
                    res = await self.ch_simulator.call("set_simulator_state", {"running": sim_state.running})
                    is_running = bool(res.get("running", False))
                except Exception as e:
                    logger.error(f"Error setting simulator state via IPC: {e}")
                    is_running = False
            else:
                if sim_state.running:
                    self.local_simulator.start()
                else:
                    self.local_simulator.stop()
                is_running = self.local_simulator.is_running()

            payload = {"type": "simulator_state", "running": is_running}
            await self._broadcast_payload(payload)
            return {"status": "success", "running": is_running}

        @app.post("/api/simulator/start")
        async def start_simulator():
            return await set_sim_state(SimulatorState(running=True))

        @app.post("/api/simulator/stop")
        async def stop_simulator():
            return await set_sim_state(SimulatorState(running=False))

        # --- 无线电日志接口 ---
        @app.post("/api/radio_log")
        async def add_radio_log(log: RadioLog):
            logger.info(f"Incoming radio log from {log.source}: type={log.type}, text={log.text}")
            payload = {
                "type": "radio_log",
                "source": log.source,
                "msg_type": log.type,
                "data": log.data,
                "text": log.text,
                "timestamp": log.timestamp if log.timestamp is not None else time.time(),
            }
            self.radio_logs.append(payload)
            if len(self.radio_logs) > 1000:
                self.radio_logs.pop(0)
            await self._broadcast_payload(payload)
            return {"status": "success"}

        @app.get("/api/radio_log")
        @app.get("/api/radio_logs")
        async def get_radio_logs(limit: int = 50, offset: Optional[int] = None):
            """获取无线电通信日志历史（供 CLI 工具与前端查询）"""
            total = len(self.radio_logs)
            if limit is not None and limit > 0:
                if offset is not None and offset >= 0:
                    logs = self.radio_logs[offset : offset + limit]
                else:
                    logs = self.radio_logs[-limit:]
            else:
                if offset is not None and offset >= 0:
                    logs = self.radio_logs[offset:]
                else:
                    logs = list(self.radio_logs)
            return {
                "status": "success",
                "total": total,
                "count": len(logs),
                "logs": logs,
            }

        # --- AI 接口连通性测试代理接口 ---
        @app.get("/api/controller/test_ai")
        @app.post("/api/controller/test_ai")
        async def proxy_controller_test_ai(timeout: float = 30.0):
            """代理向 Controller 进程发送 AI 连通性测试请求（优先 IPC）"""
            if self.ch_controller:
                try:
                    res = await self.ch_controller.call("test_ai", {"timeout": timeout}, timeout=timeout + 5.0)
                    return res
                except Exception as e:
                    logger.error(f"Failed to contact controller process via IPC: {e}")
                    return {
                        "status": "error",
                        "source": "agent_controller",
                        "available": False,
                        "error": f"Failed to contact controller process via IPC: {e}",
                    }
            # 回退/测试桩支持
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(f"{CONTROLLER_URL}/test_ai")
                    return resp.json()
            except Exception as e:
                logger.error(f"Failed to contact controller process at {CONTROLLER_URL}: {e}")
                return {
                    "status": "error",
                    "source": "agent_controller",
                    "available": False,
                    "error": f"Failed to contact controller process at {CONTROLLER_URL}: {e}",
                }

        @app.get("/api/pilot/test_ai")
        @app.post("/api/pilot/test_ai")
        async def proxy_pilot_test_ai(timeout: float = 30.0):
            """代理向 Pilot 进程发送 AI 连通性测试请求（优先 IPC）"""
            if self.ch_pilot:
                try:
                    res = await self.ch_pilot.call("test_ai", {"timeout": timeout}, timeout=timeout + 5.0)
                    return res
                except Exception as e:
                    logger.error(f"Failed to contact pilot process via IPC: {e}")
                    return {
                        "status": "error",
                        "source": "agent_pilot",
                        "available": False,
                        "error": f"Failed to contact pilot process via IPC: {e}",
                    }
            # 回退/测试桩支持
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(f"{PILOT_URL}/test_ai")
                    return resp.json()
            except Exception as e:
                logger.error(f"Failed to contact pilot process at {PILOT_URL}: {e}")
                return {
                    "status": "error",
                    "source": "agent_pilot",
                    "available": False,
                    "error": f"Failed to contact pilot process at {PILOT_URL}: {e}",
                }

        @app.post("/api/pilot/radio_RX")
        async def proxy_pilot_radio_rx(request: Request):
            """代理向 Pilot 进程发送无线电报文（优先 IPC）"""
            try:
                body = await request.json()
            except Exception:
                body = {}

            if self.ch_pilot:
                try:
                    res = await self.ch_pilot.call("radio_RX", body, timeout=60.0)
                    return res
                except Exception as e:
                    logger.error(f"Failed to contact pilot process via IPC: {e}")
                    return {
                        "status": "error",
                        "source": "agent_pilot",
                        "error": f"Failed to contact pilot process via IPC: {e}",
                    }
            # 回退/测试桩支持
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(f"{PILOT_URL}/radio_RX", json=body)
                    return resp.json()
            except Exception as e:
                logger.error(f"Failed to contact pilot process at {PILOT_URL}: {e}")
                return {
                    "status": "error",
                    "source": "agent_pilot",
                    "error": f"Failed to contact pilot process at {PILOT_URL}: {e}",
                }

        # --- 物体与运动状态接口 ---
        @app.get("/api/update")
        async def api_update_object(
            id: str,
            speed: Optional[float] = None,
            heading: Optional[float] = None,
            altitude: Optional[float] = None,
        ):
            params = {
                "id": id,
                "speed": speed,
                "heading": heading,
                "altitude": altitude
            }
            if self.ch_simulator:
                try:
                    res = await self.ch_simulator.call("update_object", params)
                    found = res.get("found", False)
                    obj = res.get("object")
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            else:
                found, obj = self.local_simulator.update_object(
                    object_id=id, speed=speed, heading=heading, altitude=altitude
                )

            if not found:
                return {"status": "error", "message": f"Object with id {id} not found"}
            if obj is None:
                return {"status": "ignored", "message": "No parameters provided to update"}

            await self.broadcast_state()
            return {"status": "success", "object": obj}

        @app.get("/api/objects")
        async def api_list_objects():
            return await self.get_objects_list()

        @app.get("/api/add")
        async def api_add_object(
            name: Optional[str] = None,
            lat: float = 0.0,
            lon: float = 0.0,
            speed: float = 10.0,
            heading: float = 360.0,
            altitude: float = 0.0,
            target_heading: Optional[float] = None,
            target_altitude: Optional[float] = None,
        ):
            params = {
                "name": name,
                "lat": lat,
                "lon": lon,
                "speed": speed,
                "heading": heading,
                "altitude": altitude,
                "target_heading": target_heading,
                "target_altitude": target_altitude,
            }
            if self.ch_simulator:
                try:
                    new_obj = await self.ch_simulator.call("add_object", params)
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            else:
                new_obj = self.local_simulator.add_object(**params)

            await self.broadcast_state()
            return {"status": "success", "object": new_obj}

        @app.api_route("/api/delete", methods=["GET", "POST", "DELETE"])
        async def api_delete_object(request: Request, id: Optional[str] = None):
            req_id = id
            if not req_id:
                req_id = request.query_params.get("id")
            if not req_id:
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        req_id = body.get("id")
                except Exception:
                    pass
            if req_id is not None:
                req_id = str(req_id).strip()
            if not req_id:
                return {"status": "error", "message": "Missing 'id' parameter"}

            if self.ch_simulator:
                try:
                    removed = await self.ch_simulator.call("remove_object", {"id": req_id})
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            else:
                removed = self.local_simulator.remove_object(req_id)

            if not removed:
                return {"status": "error", "message": f"Object with id {req_id} not found"}

            await self.broadcast_state()
            return {"status": "success", "message": f"Object {req_id} removed", "id": req_id}

        # --- WebSocket 交互端点 ---
        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            self.clients.append(websocket)

            # 首次连接下推当前全部状态快照
            current_objs = await self.get_objects_list()
            await websocket.send_json({"type": "state", "objects": current_objs})

            if self.current_airport.get("lat") is not None:
                await websocket.send_json({
                    "type": "airport",
                    "lat": self.current_airport.get("lat"),
                    "lon": self.current_airport.get("lon"),
                    "runway_heading": self.current_airport.get("runway_heading")
                })

            await websocket.send_json({"type": "controller_state", "enabled": self.controller_enabled})
            is_running = await self.is_simulator_running()
            await websocket.send_json({"type": "simulator_state", "running": is_running})

            # 首次连接下发最近的无线电通信历史记录
            for r_log in self.radio_logs[-30:]:
                try:
                    await websocket.send_json(r_log)
                except Exception:
                    pass

            try:
                while True:
                    message = await websocket.receive_json()
                    msg_type = message.get("type")
                    if msg_type == "add":
                        params = {
                            "name": message.get("name"),
                            "lat": _to_float(message.get("lat"), 0.0),
                            "lon": _to_float(message.get("lon"), 0.0),
                            "speed": _to_float(message.get("speed_knots"), 10.0),
                            "heading": _to_float(message.get("heading"), 360.0),
                            "altitude": _to_float(message.get("altitude_ft"), 0.0),
                            "vertical_speed_ft_min": _to_float(message.get("vertical_speed_ft_min"), 0.0),
                            "turn_rate_deg_sec": _to_float(message.get("turn_rate_deg_sec"), 3.0),
                            "target_heading": _to_float(message["target_heading"]) if message.get("target_heading") not in (None, "") else None,
                            "target_altitude": _to_float(message["target_altitude_ft"]) if message.get("target_altitude_ft") not in (None, "") else None,
                        }
                        if self.ch_simulator:
                            await self.ch_simulator.call("add_object", params)
                        else:
                            self.local_simulator.add_object(**params)
                        await self.broadcast_state()

                    elif msg_type == "update":
                        params = {
                            "id": message.get("id"),
                            "speed_knots": _to_float(message["speed_knots"]) if "speed_knots" in message else None,
                            "altitude_ft": _to_float(message["altitude_ft"]) if "altitude_ft" in message else None,
                            "vertical_speed_ft_min": _to_float(message["vertical_speed_ft_min"]) if "vertical_speed_ft_min" in message else None,
                            "target_altitude_ft": _to_float(message["target_altitude_ft"]) if message.get("target_altitude_ft") not in (None, "") else None,
                            "target_heading": _to_float(message["target_heading"]) if message.get("target_heading") not in (None, "") else None,
                            "turn_rate_deg_sec": _to_float(message["turn_rate_deg_sec"]) if "turn_rate_deg_sec" in message else None,
                            "current_heading": _to_float(message["heading"]) if "heading" in message else None,
                            "name": message.get("name"),
                        }
                        if self.ch_simulator:
                            await self.ch_simulator.call("update_object", params)
                        else:
                            self.local_simulator.update_object(**params)
                        await self.broadcast_state()

                    elif msg_type == "remove":
                        obj_id = message.get("id")
                        if obj_id:
                            if self.ch_simulator:
                                await self.ch_simulator.call("remove_object", {"id": obj_id})
                            else:
                                self.local_simulator.remove_object(obj_id)
                        await self.broadcast_state()

            except WebSocketDisconnect:
                if websocket in self.clients:
                    self.clients.remove(websocket)

    def _get_active_channels(self) -> List[IPCChannel]:
        """获取所有唯一已初始化的 IPCChannel 实例集合"""
        ch_set = set()
        for ch in [self.ch_radio_sim, self.ch_flight_sim, self.ch_flight_ctrl, self.ch_flight_pilot, self.ch_flight_master]:
            if ch is not None:
                ch_set.add(ch)
        return list(ch_set)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动所有内部 IPC 管道监听"""
        cur_loop = loop
        if cur_loop is None:
            try:
                cur_loop = asyncio.get_running_loop()
            except RuntimeError:
                cur_loop = None
        for ch in self._get_active_channels():
            ch.start(loop=cur_loop)

    def run(self) -> None:
        """启动 Uvicorn Web 服务"""
        import uvicorn
        config = uvicorn.Config(app=self.app, host=self.host, port=self.port, log_level="info", log_config=None)
        self._server = uvicorn.Server(config)
        self._server.run()

    def stop(self) -> None:
        """关闭服务与通道连接"""
        if self._server is not None:
            self._server.should_exit = True
        if self.sync_task:
            self.sync_task.cancel()
        for ch in self._get_active_channels():
            ch.close()
        if hasattr(self, "ipc_log_handler") and self.ipc_log_handler:
            self.ipc_log_handler.set_channel(None)


def webview_process_entrypoint(
    conn_simulator: Optional[Connection] = None,
    conn_controller: Optional[Connection] = None,
    conn_pilot: Optional[Connection] = None,
    conn_master: Optional[Connection] = None,
    conn_radio_simulator: Optional[Connection] = None,
    conn_flight_simulator: Optional[Connection] = None,
    conn_flight_controller: Optional[Connection] = None,
    conn_flight_pilot: Optional[Connection] = None,
    conn_flight_master: Optional[Connection] = None,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的函数入口"""
    app_instance = WebviewApp(
        host=host,
        port=port,
        conn_simulator=conn_simulator,
        conn_controller=conn_controller,
        conn_pilot=conn_pilot,
        conn_master=conn_master,
        conn_radio_simulator=conn_radio_simulator,
        conn_flight_simulator=conn_flight_simulator,
        conn_flight_controller=conn_flight_controller,
        conn_flight_pilot=conn_flight_pilot,
        conn_flight_master=conn_flight_master,
    )
    app_instance.run()


# 单例与向后兼容接口（保留供既有单进程测试使用）
webview_instance = WebviewApp()
app = webview_instance.app
airplane_sim = webview_instance.local_simulator

def get_webview_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info", log_config=None)
    return uvicorn.Server(config)

def run_webview(host: str = "0.0.0.0", port: int = 8000):
    instance = WebviewApp(host=host, port=port)
    instance.run()

def stop_webview():
    webview_instance.stop()
