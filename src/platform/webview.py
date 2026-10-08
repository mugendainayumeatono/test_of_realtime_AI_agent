import asyncio
import copy
import httpx
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List


log_dir = Path("../../logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "platform.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("platform.webview")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from airplane_simulator import (
    simulator as airplane_sim,
    _to_float,
    _normalize_heading,
)

app = FastAPI()
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
TEMPLATE_FILE = Path(__file__).resolve().parent / "templates" / "index.html"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

def get_persisted_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"airport": {"lat": None, "lon": None, "runway_heading": None}, "controller_enabled": False}

def save_persisted_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

clients: List[WebSocket] = []
sync_task: asyncio.Task | None = None


async def broadcast_state() -> None:
    """向所有连接的客户端广播当前所有物体的最新状态快照"""
    if not clients:
        return
    current_objs = airplane_sim.get_objects()
    payload = {"type": "state", "objects": current_objs}
    disconnected: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            disconnected.append(ws)
        except WebSocketDisconnect:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in clients:
            clients.remove(ws)


async def sync_loop() -> None:
    """
    Webview 状态同步循环。
    仅负责从 airplane_simulator 线程读取最新数据，并在仿真处于运行状态时推送到网页端。
    """
    while True:
        await asyncio.sleep(0.5)
        if airplane_sim.is_running() and clients:
            await broadcast_state()


@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Webview service starting up inside Platform thread.")
    # 进程启动时，强制将 controller_enabled 初始化为 False
    state = get_persisted_state()
    state["controller_enabled"] = False
    save_persisted_state(state)
    logger.info("Initialized controller_state to enabled=False on startup.")

    # 启动与网页端的实时状态广播同步任务（airplane_simulator 线程不在此处自动启动）
    global sync_task
    if sync_task is None:
        sync_task = asyncio.create_task(sync_loop())


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
class AirportLocation(BaseModel):
    lat: float
    lon: float
    runway_heading: float | None = None

@app.get("/api/airport")
async def get_airport():
    state = get_persisted_state()
    return state.get("airport", {"lat": None, "lon": None})

@app.post("/api/airport")
async def set_airport(loc: AirportLocation):
    logger.info(f"Setting airport location to lat={loc.lat}, lon={loc.lon}, runway_heading={loc.runway_heading}")
    data = {"lat": loc.lat, "lon": loc.lon, "runway_heading": loc.runway_heading}
    state = get_persisted_state()
    state["airport"] = data
    save_persisted_state(state)
    
    payload = {"type": "airport", "lat": loc.lat, "lon": loc.lon, "runway_heading": loc.runway_heading}
    disconnected: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            disconnected.append(ws)
        except WebSocketDisconnect:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in clients:
            clients.remove(ws)
    return {"status": "success", "airport": data}


# --- AI 管制员状态接口 ---
class ControllerState(BaseModel):
    enabled: bool

@app.get("/api/controller_state")
async def get_controller_state():
    state = get_persisted_state()
    return {"enabled": state.get("controller_enabled", False)}

@app.post("/api/controller_state")
async def set_controller_state(c_state: ControllerState):
    logger.info(f"Setting controller state to enabled={c_state.enabled}")
    state = get_persisted_state()
    state["controller_enabled"] = c_state.enabled
    save_persisted_state(state)

    payload = {"type": "controller_state", "enabled": c_state.enabled}
    disconnected: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            disconnected.append(ws)
        except WebSocketDisconnect:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in clients:
            clients.remove(ws)
    return {"status": "success", "enabled": c_state.enabled}


# --- airplane_simulator 线程控制接口 ---
class SimulatorState(BaseModel):
    running: bool

@app.get("/api/simulator/state")
async def get_simulator_state():
    """查询 airplane_simulator 线程当前的运行状态"""
    return {"running": airplane_sim.is_running()}

@app.post("/api/simulator/state")
async def set_simulator_state(sim_state: SimulatorState):
    """控制启动或停止 airplane_simulator 线程，并向网页端广播"""
    logger.info(f"Setting airplane_simulator state to running={sim_state.running}")
    if sim_state.running:
        airplane_sim.start()
    else:
        airplane_sim.stop()
    is_running = airplane_sim.is_running()

    payload = {"type": "simulator_state", "running": is_running}
    disconnected: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            disconnected.append(ws)
        except WebSocketDisconnect:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in clients:
            clients.remove(ws)
    return {"status": "success", "running": is_running}

@app.post("/api/simulator/start")
async def start_simulator():
    """启动 airplane_simulator 线程"""
    return await set_simulator_state(SimulatorState(running=True))

@app.post("/api/simulator/stop")
async def stop_simulator():
    """停止 airplane_simulator 线程"""
    return await set_simulator_state(SimulatorState(running=False))


# --- 无线电日志接口 ---
class RadioLog(BaseModel):
    source: str
    type: str
    data: dict | None = None
    text: str | None = None

@app.post("/api/radio_log")
async def add_radio_log(log: RadioLog):
    logger.info(f"Incoming radio log from {log.source}: type={log.type}, text={log.text}")
    payload = {
        "type": "radio_log",
        "source": log.source,
        "msg_type": log.type,
        "data": log.data,
        "text": log.text,
        "timestamp": time.time()
    }
    disconnected: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            disconnected.append(ws)
        except WebSocketDisconnect:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in clients:
            clients.remove(ws)
    return {"status": "success"}


# --- AI 接口连通性测试代理接口 ---
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:8030")
PILOT_URL = os.environ.get("PILOT_URL", "http://127.0.0.1:8020")

@app.get("/api/controller/test_ai")
@app.post("/api/controller/test_ai")
async def proxy_controller_test_ai(timeout: float = 30.0):
    """代理向 Controller 进程发送 AI 连通性测试请求"""
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
            "error": f"Failed to contact controller process at {CONTROLLER_URL}: {e}"
        }

@app.get("/api/pilot/test_ai")
@app.post("/api/pilot/test_ai")
async def proxy_pilot_test_ai(timeout: float = 30.0):
    """代理向 Pilot 进程发送 AI 连通性测试请求"""
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
            "error": f"Failed to contact pilot process at {PILOT_URL}: {e}"
        }


@app.post("/api/pilot/radio_RX")
async def proxy_pilot_radio_rx(request: Request):
    """代理向 Pilot 进程发送无线电报文"""
    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{PILOT_URL}/radio_RX", json=body)
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to contact pilot process at {PILOT_URL}: {e}")
        return {
            "status": "error",
            "source": "agent_pilot",
            "error": f"Failed to contact pilot process at {PILOT_URL}: {e}"
        }


# --- 物体与运动状态接口（委托 airplane_simulator 处理）---
@app.get("/api/update")
async def api_update_object(
    id: str,
    speed: float | None = None,
    heading: float | None = None,
    altitude: float | None = None,
):
    """向 airplane_simulator 下达运动参数修改指令"""
    found, obj = airplane_sim.update_object(
        object_id=id,
        speed=speed,
        heading=heading,
        altitude=altitude
    )
    if not found:
        return {"status": "error", "message": f"Object with id {id} not found"}
    if obj is None:
        return {"status": "ignored", "message": "No parameters provided to update"}
    
    await broadcast_state()
    return {"status": "success", "object": obj}


@app.get("/api/objects")
async def api_list_objects():
    """从 airplane_simulator 读取当前所有物体的最新运动状态"""
    return airplane_sim.get_objects()


@app.get("/api/add")
async def api_add_object(
    name: str | None = None,
    lat: float = 0.0,
    lon: float = 0.0,
    speed: float = 10.0,
    heading: float = 360.0,
    altitude: float = 0.0,
    target_heading: float | None = None,
    target_altitude: float | None = None,
):
    """向 airplane_simulator 添加新飞行器"""
    new_obj = airplane_sim.add_object(
        name=name,
        lat=lat,
        lon=lon,
        speed=speed,
        heading=heading,
        altitude=altitude,
        target_heading=target_heading,
        target_altitude=target_altitude,
    )
    await broadcast_state()
    return {"status": "success", "object": new_obj}


@app.api_route("/api/delete", methods=["GET", "POST", "DELETE"])
async def api_delete_object(request: Request, id: str | None = None):
    """向 airplane_simulator 下达移除指定飞行器指令 (GET / POST / DELETE)"""
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
    removed = airplane_sim.remove_object(req_id)
    if not removed:
        return {"status": "error", "message": f"Object with id {req_id} not found"}
    await broadcast_state()
    return {"status": "success", "message": f"Object {req_id} removed", "id": req_id}


# --- WebSocket 交互端点 ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.append(websocket)

    # 首次连接下推当前全部状态快照
    await websocket.send_json({"type": "state", "objects": airplane_sim.get_objects()})
    
    state = get_persisted_state()
    airport_data = state.get("airport", {})
    if airport_data.get("lat") is not None:
        await websocket.send_json({
            "type": "airport",
            "lat": airport_data.get("lat"),
            "lon": airport_data.get("lon"),
            "runway_heading": airport_data.get("runway_heading")
        })
    
    await websocket.send_json({"type": "controller_state", "enabled": state.get("controller_enabled", False)})
    await websocket.send_json({"type": "simulator_state", "running": airplane_sim.is_running()})

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type == "add":
                lat = _to_float(message.get("lat"), 0.0)
                lon = _to_float(message.get("lon"), 0.0)
                name = message.get("name")
                speed_knots = _to_float(message.get("speed_knots"), 10.0)
                heading = _to_float(message.get("heading"), 360.0)
                altitude_ft = _to_float(message.get("altitude_ft"), 0.0)
                vertical_speed_ft_min = _to_float(message.get("vertical_speed_ft_min"), 0.0)
                turn_rate_deg_sec = _to_float(message.get("turn_rate_deg_sec"), 3.0)

                target_altitude_ft = None
                if "target_altitude_ft" in message and message["target_altitude_ft"] not in (None, ""):
                    target_altitude_ft = _to_float(message["target_altitude_ft"])

                target_heading = None
                if "target_heading" in message and message["target_heading"] not in (None, ""):
                    target_heading = _to_float(message["target_heading"])

                airplane_sim.add_object(
                    name=name,
                    lat=lat,
                    lon=lon,
                    speed=speed_knots,
                    heading=heading,
                    altitude=altitude_ft,
                    vertical_speed_ft_min=vertical_speed_ft_min,
                    turn_rate_deg_sec=turn_rate_deg_sec,
                    target_heading=target_heading,
                    target_altitude=target_altitude_ft,
                )
                await broadcast_state()

            elif msg_type == "update":
                object_id = message.get("id")
                target_alt = None
                if "target_altitude_ft" in message and message["target_altitude_ft"] not in (None, ""):
                    target_alt = _to_float(message["target_altitude_ft"])
                
                target_hdg = None
                if "target_heading" in message and message["target_heading"] not in (None, ""):
                    target_hdg = _to_float(message["target_heading"])

                airplane_sim.update_object(
                    object_id=object_id,
                    speed_knots=_to_float(message["speed_knots"]) if "speed_knots" in message else None,
                    altitude_ft=_to_float(message["altitude_ft"]) if "altitude_ft" in message else None,
                    vertical_speed_ft_min=_to_float(message["vertical_speed_ft_min"]) if "vertical_speed_ft_min" in message else None,
                    target_altitude_ft=target_alt,
                    target_heading=target_hdg,
                    turn_rate_deg_sec=_to_float(message["turn_rate_deg_sec"]) if "turn_rate_deg_sec" in message else None,
                    current_heading=_to_float(message["heading"]) if "heading" in message else None,
                    name=message.get("name"),
                )
                await broadcast_state()

            elif msg_type == "remove":
                object_id = message.get("id")
                if object_id:
                    airplane_sim.remove_object(object_id)
                await broadcast_state()

    except WebSocketDisconnect:
        if websocket in clients:
            clients.remove(websocket)


# --- 线程执行与生命周期支持 ---
_current_server = None

def get_webview_server(host: str = "0.0.0.0", port: int = 8000):
    global _current_server
    import uvicorn
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    _current_server = uvicorn.Server(config)
    return _current_server

def run_webview(host: str = "0.0.0.0", port: int = 8000):
    server = get_webview_server(host=host, port=port)
    server.run()

def stop_webview():
    global _current_server
    if _current_server is not None:
        _current_server.should_exit = True
