import asyncio
import math
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
TEMPLATE_FILE = Path(__file__).resolve().parent / "templates" / "index.html"

objects: Dict[str, Dict] = {}
clients: List[WebSocket] = []
state_lock = asyncio.Lock()
update_task: asyncio.Task | None = None

EARTH_RADIUS_M = 6_371_000
KNOT_TO_M_PER_S = 0.514444


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_heading(value, default=0.0):
    heading = _to_float(value, default)
    if heading == 360.0:
        heading = 0.0
    return heading % 360


def _move_object(obj: Dict, dt: float) -> bool:
    moved = False
    speed_knots = _to_float(obj.get("speed_knots", 0.0))
    vertical_speed_ft_min = _to_float(obj.get("vertical_speed_ft_min", 0.0))
    if speed_knots != 0.0:
        distance = speed_knots * KNOT_TO_M_PER_S * dt
        heading_rad = math.radians(_normalize_heading(obj.get("heading", 0.0)))
        lat1 = math.radians(obj["lat"])
        lon1 = math.radians(obj["lon"])
        angular_distance = distance / EARTH_RADIUS_M
        lat2 = math.asin(
            math.sin(lat1) * math.cos(angular_distance)
            + math.cos(lat1) * math.sin(angular_distance) * math.cos(heading_rad)
        )
        lon2 = lon1 + math.atan2(
            math.sin(heading_rad) * math.sin(angular_distance) * math.cos(lat1),
            math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
        )
        obj["lat"] = math.degrees(lat2)
        obj["lon"] = math.degrees(lon2)
        moved = True

    altitude_ft = _to_float(obj.get("altitude_ft", 0.0))
    target_altitude_ft = obj.get("target_altitude_ft")
    v_speed_setting = _to_float(obj.get("vertical_speed_ft_min", 0.0))

    if target_altitude_ft is not None:
        target_altitude_ft = _to_float(target_altitude_ft)
        if altitude_ft != target_altitude_ft:
            # Use specified speed if non-zero, otherwise default to 400
            effective_rate = abs(v_speed_setting) if v_speed_setting != 0.0 else 400.0
            diff = target_altitude_ft - altitude_ft
            max_change = (effective_rate * dt) / 60.0
            
            if abs(diff) <= max_change:
                altitude_ft = target_altitude_ft
            else:
                altitude_ft += math.copysign(max_change, diff)
            moved = True
    else:
        if v_speed_setting != 0.0:
            altitude_ft = altitude_ft + v_speed_setting * dt / 60.0
            moved = True

    obj["altitude_ft"] = altitude_ft
    obj.pop("altitude_in", None)

    # heading logic
    current_heading = _normalize_heading(obj.get("heading", 0.0))
    target_heading = obj.get("target_heading")
    if target_heading is not None:
        target_heading = _normalize_heading(target_heading)
        if current_heading != target_heading:
            turn_rate = _to_float(obj.get("turn_rate_deg_sec", 3.0))
            if turn_rate > 0.0:
                diff = (target_heading - current_heading + 360) % 360
                if diff > 180:
                    diff -= 360  # negative means turn left
                
                max_turn = turn_rate * dt
                if abs(diff) <= max_turn:
                    current_heading = target_heading
                else:
                    current_heading = current_heading + math.copysign(max_turn, diff)
                
                moved = True

    obj["heading"] = _normalize_heading(current_heading)
    return moved


async def broadcast_state() -> None:
    if not clients:
        return
    payload = {"type": "state", "objects": list(objects.values())}
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


async def update_loop() -> None:
    last_time = time.time()
    while True:
        await asyncio.sleep(0.5)
        now = time.time()
        dt = now - last_time
        last_time = now
        async with state_lock:
            updated = False
            for obj in objects.values():
                if _move_object(obj, dt):
                    updated = True
        if updated:
            await broadcast_state()


@app.on_event("startup")
async def startup_event() -> None:
    global update_task
    if update_task is None:
        update_task = asyncio.create_task(update_loop())


@app.get("/", response_class=HTMLResponse)
async def get_root(request: Request) -> HTMLResponse:
    return HTMLResponse(TEMPLATE_FILE.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    async with state_lock:
        clients.append(websocket)
        await websocket.send_json({"type": "state", "objects": list(objects.values())})
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            async with state_lock:
                if msg_type == "add":
                    lat = _to_float(message.get("lat"), 0.0)
                    lon = _to_float(message.get("lon"), 0.0)
                    object_id = str(uuid.uuid4())
                    objects[object_id] = {
                        "id": object_id,
                        "name": message.get("name") or f"object-{object_id[:8]}",
                        "lat": lat,
                        "lon": lon,
                        "heading": _normalize_heading(message.get("heading", random.uniform(0, 360))),
                        "speed_knots": _to_float(message.get("speed_knots", 10.0)),
                        "altitude_ft": _to_float(message.get("altitude_ft", 0.0)),
                        "vertical_speed_ft_min": _to_float(message.get("vertical_speed_ft_min", 0.0)),
                    }
                    if "target_altitude_ft" in message:
                        if message["target_altitude_ft"] in (None, ""):
                            objects[object_id]["target_altitude_ft"] = None
                        else:
                            objects[object_id]["target_altitude_ft"] = _to_float(message["target_altitude_ft"])
                    
                    if "target_heading" in message:
                        if message["target_heading"] in (None, ""):
                            objects[object_id]["target_heading"] = None
                        else:
                            objects[object_id]["target_heading"] = _to_float(message["target_heading"])
                            
                    if "turn_rate_deg_sec" in message:
                        objects[object_id]["turn_rate_deg_sec"] = _to_float(message["turn_rate_deg_sec"], 3.0)

                elif msg_type == "update":
                    object_id = message.get("id")
                    obj = objects.get(object_id)
                    if obj is not None:
                        if "speed_knots" in message:
                            obj["speed_knots"] = _to_float(message["speed_knots"], obj["speed_knots"])
                        if "altitude_ft" in message:
                            obj["altitude_ft"] = _to_float(message["altitude_ft"], obj.get("altitude_ft", obj.get("altitude_in", 0.0) / 12))
                            obj.pop("altitude_in", None)
                        if "vertical_speed_ft_min" in message:
                            obj["vertical_speed_ft_min"] = _to_float(message["vertical_speed_ft_min"], obj["vertical_speed_ft_min"])
                        if "target_altitude_ft" in message:
                            if message["target_altitude_ft"] in (None, ""):
                                obj["target_altitude_ft"] = None
                            else:
                                obj["target_altitude_ft"] = _to_float(message["target_altitude_ft"])
                        
                        if "target_heading" in message:
                            if message["target_heading"] in (None, ""):
                                obj["target_heading"] = None
                            else:
                                obj["target_heading"] = _to_float(message["target_heading"])
                                
                        if "turn_rate_deg_sec" in message:
                            obj["turn_rate_deg_sec"] = _to_float(message["turn_rate_deg_sec"], obj.get("turn_rate_deg_sec", 3.0))

                        if "heading" in message:
                            obj["heading"] = _normalize_heading(message["heading"], obj["heading"])
                        if "name" in message:
                            obj["name"] = message["name"] or obj["name"]
                elif msg_type == "remove":
                    object_id = message.get("id")
                    objects.pop(object_id, None)
            await broadcast_state()
    except WebSocketDisconnect:
        if websocket in clients:
            clients.remove(websocket)
