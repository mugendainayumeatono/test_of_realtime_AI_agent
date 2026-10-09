"""
src/platform/airplane_simulator.py

飞机运动学推算与状态管理模块。
彻底对象化设计：
1. AirplaneSimulator: 飞行器实体运动学解算与物理状态管理类（大圆航线推算、加减速、爬升率与转弯率积分）
2. AirplaneSimulatorService: 独立进程服务封装类，通过 multiprocessing.Pipe 暴露 IPC 通信接口，
   服务于 Webview 进程、Pilot 飞行员进程及 Master 进程。
"""

import copy
import logging
import math
import os
import signal
import sys
import threading
import time
import uuid
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保能引用 agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ipc import IPCChannel

log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("platform.airplane_simulator")

EARTH_RADIUS_M = 6_371_000
KNOT_TO_M_PER_S = 0.514444


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_heading(value: Any, default: float = 0.0) -> float:
    heading = _to_float(value, default)
    if heading == 360.0:
        heading = 0.0
    return heading % 360.0


def _move_object(obj: Dict[str, Any], dt: float) -> bool:
    """计算单个飞行器在 dt 时间间隔内的空间运动"""
    moved = False
    speed_knots = _to_float(obj.get("speed_knots", 0.0))
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

    # 航向更新
    current_heading = _normalize_heading(obj.get("heading", 0.0))
    target_heading = obj.get("target_heading")
    if target_heading is not None:
        target_heading = _normalize_heading(target_heading)
        if current_heading != target_heading:
            turn_rate = _to_float(obj.get("turn_rate_deg_sec", 3.0))
            if turn_rate > 0.0:
                diff = (target_heading - current_heading + 360) % 360
                if diff > 180:
                    diff -= 360

                max_turn = turn_rate * dt
                if abs(diff) <= max_turn:
                    current_heading = target_heading
                else:
                    current_heading = current_heading + math.copysign(max_turn, diff)

                moved = True

    obj["heading"] = _normalize_heading(current_heading)
    return moved


class AirplaneSimulator:
    """
    飞机运动仿真解算核心对象。
    管理飞行器实体并在内部解算循环中执行 0.5s 物理积分解算。
    """

    def __init__(self):
        self._objects: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """启动仿真推算主循环。若已在运行则返回 False。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning("airplane_simulator thread is already running.")
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="airplane_simulator",
                daemon=True
            )
            self._thread.start()
            logger.info("airplane_simulator update loop successfully started.")
            return True

    def stop(self) -> bool:
        """停止仿真推算主循环。若未运行则返回 False。"""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                logger.warning("airplane_simulator is not running.")
                return False
            self._stop_event.set()
            thread = self._thread

        thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
        logger.info("airplane_simulator update loop successfully stopped.")
        return True

    def is_running(self) -> bool:
        """检查仿真循环是否正在运行。"""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """运动推算主循环 (约 0.5s/tick)"""
        logger.info("airplane_simulator update loop running...")
        last_time = time.time()
        while not self._stop_event.is_set():
            if self._stop_event.wait(0.5):
                break
            now = time.time()
            dt = now - last_time
            last_time = now

            with self._lock:
                for obj in self._objects.values():
                    _move_object(obj, dt)
        logger.info("airplane_simulator update loop terminated.")

    # --- 运动状态读取接口 ---
    def get_objects(self) -> List[Dict[str, Any]]:
        """获取当前所有物体的实时运动状态快照。"""
        with self._lock:
            return [copy.deepcopy(obj) for obj in self._objects.values()]

    def get_object(self, object_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取单个物体的运动状态快照。"""
        with self._lock:
            obj = self._objects.get(object_id)
            return copy.deepcopy(obj) if obj else None

    def get_motion_state(self) -> List[Dict[str, Any]]:
        """读取当前全量运动状态的统一接口（别名）。"""
        return self.get_objects()

    # --- 运动状态修改接口 (增删改) ---
    def add_object(
        self,
        name: Optional[str] = None,
        lat: float = 0.0,
        lon: float = 0.0,
        speed: float = 10.0,
        heading: float = 360.0,
        altitude: float = 0.0,
        target_heading: Optional[float] = None,
        target_altitude: Optional[float] = None,
        vertical_speed_ft_min: float = 0.0,
        turn_rate_deg_sec: float = 3.0,
    ) -> Dict[str, Any]:
        """添加新物体并初始化其运动学参数。"""
        object_id = str(uuid.uuid4())
        obj_name = name or f"object-{object_id[:8]}"
        new_obj = {
            "id": object_id,
            "name": obj_name,
            "lat": _to_float(lat, 0.0),
            "lon": _to_float(lon, 0.0),
            "heading": _normalize_heading(heading, 360.0),
            "speed_knots": _to_float(speed, 10.0),
            "altitude_ft": _to_float(altitude, 0.0),
            "vertical_speed_ft_min": _to_float(vertical_speed_ft_min, 0.0),
            "turn_rate_deg_sec": _to_float(turn_rate_deg_sec, 3.0),
            "target_heading": _normalize_heading(target_heading) if target_heading is not None else 360.0,
            "target_altitude_ft": _to_float(target_altitude) if target_altitude is not None else 0.0,
        }
        with self._lock:
            self._objects[object_id] = new_obj
        logger.info(f"Added object: id={object_id}, name={obj_name}, lat={new_obj['lat']}, lon={new_obj['lon']}")
        return copy.deepcopy(new_obj)

    def update_object(
        self,
        object_id: str,
        speed: Optional[float] = None,
        heading: Optional[float] = None,
        altitude: Optional[float] = None,
        speed_knots: Optional[float] = None,
        altitude_ft: Optional[float] = None,
        vertical_speed_ft_min: Optional[float] = None,
        target_heading: Optional[float] = None,
        target_altitude_ft: Optional[float] = None,
        turn_rate_deg_sec: Optional[float] = None,
        current_heading: Optional[float] = None,
        name: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        修改指定物体的运动状态参数。
        返回 (found, updated_obj)。若物体不存在返回 (False, None)；若无更新参数返回 (True, None)。
        """
        with self._lock:
            obj = self._objects.get(object_id)
            if obj is None:
                return False, None

            updated = False
            # 航速修改
            if speed_knots is not None:
                obj["speed_knots"] = _to_float(speed_knots)
                updated = True
            elif speed is not None:
                obj["speed_knots"] = _to_float(speed)
                updated = True

            # 高度修改
            if altitude_ft is not None:
                obj["altitude_ft"] = _to_float(altitude_ft)
                obj.pop("altitude_in", None)
                updated = True
            if target_altitude_ft is not None:
                obj["target_altitude_ft"] = _to_float(target_altitude_ft)
                updated = True
            elif altitude is not None:
                obj["target_altitude_ft"] = _to_float(altitude)
                updated = True

            # 航向修改
            if current_heading is not None:
                obj["heading"] = _normalize_heading(current_heading)
                updated = True
            if target_heading is not None:
                obj["target_heading"] = _normalize_heading(target_heading)
                updated = True
            elif heading is not None:
                obj["target_heading"] = _normalize_heading(heading)
                updated = True

            # 垂直速率与转弯率
            if vertical_speed_ft_min is not None:
                obj["vertical_speed_ft_min"] = _to_float(vertical_speed_ft_min)
                updated = True
            if turn_rate_deg_sec is not None:
                obj["turn_rate_deg_sec"] = _to_float(turn_rate_deg_sec)
                updated = True

            # 名称修改
            if name is not None:
                obj["name"] = name
                updated = True

            if not updated:
                return True, None

            logger.info(f"Updated object {object_id}: speed={obj.get('speed_knots')}, target_heading={obj.get('target_heading')}, target_alt={obj.get('target_altitude_ft')}")
            return True, copy.deepcopy(obj)

    def set_speed(self, object_id: str, speed_knots: float) -> bool:
        """设定指定物体的航速（节）。"""
        found, obj = self.update_object(object_id, speed_knots=speed_knots)
        return found and obj is not None

    def set_heading(self, object_id: str, heading: float, as_target: bool = True) -> bool:
        """设定指定物体的目标航向或当前航向（度）。"""
        if as_target:
            found, obj = self.update_object(object_id, target_heading=heading)
        else:
            found, obj = self.update_object(object_id, current_heading=heading)
        return found and obj is not None

    def set_altitude(self, object_id: str, altitude_ft: float, as_target: bool = True) -> bool:
        """设定指定物体的目标高度或当前高度（英尺）。"""
        if as_target:
            found, obj = self.update_object(object_id, target_altitude_ft=altitude_ft)
        else:
            found, obj = self.update_object(object_id, altitude_ft=altitude_ft)
        return found and obj is not None

    def remove_object(self, object_id: str) -> bool:
        """移除指定物体。"""
        if not object_id:
            return False
        clean_id = str(object_id).strip()
        with self._lock:
            if clean_id in self._objects:
                self._objects.pop(clean_id)
                logger.info(f"Removed object: id={clean_id}")
                return True
            clean_id_lower = clean_id.lower()
            for key in list(self._objects.keys()):
                if key.lower() == clean_id_lower:
                    self._objects.pop(key)
                    logger.info(f"Removed object (case-insensitive): id={key}")
                    return True
            return False

    def clear_objects(self) -> None:
        """清空所有物体。"""
        with self._lock:
            self._objects.clear()


class AirplaneSimulatorService:
    """
    飞行模拟器独立进程服务类。
    管理 AirplaneSimulator 实例并通过 multiprocessing.Pipe 提供全套 IPC 通信接口。
    """

    def __init__(
        self,
        conn_webview: Optional[Connection] = None,
        conn_pilot: Optional[Connection] = None,
        conn_master: Optional[Connection] = None,
    ):
        self.simulator = AirplaneSimulator()
        self._conn_webview = conn_webview
        self._conn_pilot = conn_pilot
        self._conn_master = conn_master

        self.ch_webview: Optional[IPCChannel] = None
        self.ch_pilot: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None

        self._stop_event = threading.Event()
        self._setup_channels()

    def _setup_channels(self) -> None:
        if self._conn_webview:
            self.ch_webview = IPCChannel(self._conn_webview, name="sim->webview")
            self._register_simulator_handlers(self.ch_webview)

        if self._conn_pilot:
            self.ch_pilot = IPCChannel(self._conn_pilot, name="sim->pilot")
            self._register_simulator_handlers(self.ch_pilot)

        if self._conn_master:
            self.ch_master = IPCChannel(self._conn_master, name="sim->master")
            self.ch_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_master.register_handler("status", lambda p: {
                "status": "ok",
                "running": self.simulator.is_running(),
                "pid": os.getpid(),
                "objects_count": len(self.simulator.get_objects())
            })
            self.ch_master.register_handler("stop", lambda p: self.stop())

    def _register_simulator_handlers(self, channel: IPCChannel) -> None:
        """向 IPC 通道注册模拟器所有操作命令"""
        channel.register_handler("get_objects", lambda p: self.simulator.get_objects())
        channel.register_handler("get_object", lambda p: self.simulator.get_object(p.get("id", "")))
        channel.register_handler("get_motion_state", lambda p: self.simulator.get_motion_state())
        channel.register_handler("add_object", lambda p: self.simulator.add_object(
            name=p.get("name"),
            lat=_to_float(p.get("lat"), 0.0),
            lon=_to_float(p.get("lon"), 0.0),
            speed=_to_float(p.get("speed"), 10.0),
            heading=_to_float(p.get("heading"), 360.0),
            altitude=_to_float(p.get("altitude"), 0.0),
            target_heading=p.get("target_heading"),
            target_altitude=p.get("target_altitude"),
            vertical_speed_ft_min=_to_float(p.get("vertical_speed_ft_min"), 0.0),
            turn_rate_deg_sec=_to_float(p.get("turn_rate_deg_sec"), 3.0),
        ))
        channel.register_handler("update_object", self._ipc_update_object)
        channel.register_handler("remove_object", lambda p: self.simulator.remove_object(p.get("id", "")))
        channel.register_handler("clear_objects", lambda p: (self.simulator.clear_objects(), True)[1])
        channel.register_handler("start_simulator", lambda p: {"status": "success", "running": self.simulator.start() or self.simulator.is_running()})
        channel.register_handler("stop_simulator", lambda p: {"status": "success", "running": not self.simulator.stop() and self.simulator.is_running()})
        channel.register_handler("get_simulator_state", lambda p: {"running": self.simulator.is_running()})
        channel.register_handler("set_simulator_state", lambda p: self._ipc_set_simulator_state(p))
        channel.register_handler("set_speed", lambda p: self.simulator.set_speed(p["id"], _to_float(p["speed_knots"])))
        channel.register_handler("set_heading", lambda p: self.simulator.set_heading(p["id"], _to_float(p["heading"]), p.get("as_target", True)))
        channel.register_handler("set_altitude", lambda p: self.simulator.set_altitude(p["id"], _to_float(p["altitude_ft"]), p.get("as_target", True)))

    def _ipc_update_object(self, p: Dict[str, Any]) -> Dict[str, Any]:
        object_id = p.get("id") or p.get("object_id")
        found, obj = self.simulator.update_object(
            object_id=object_id,
            speed=p.get("speed"),
            heading=p.get("heading"),
            altitude=p.get("altitude"),
            speed_knots=p.get("speed_knots"),
            altitude_ft=p.get("altitude_ft"),
            vertical_speed_ft_min=p.get("vertical_speed_ft_min"),
            target_heading=p.get("target_heading"),
            target_altitude_ft=p.get("target_altitude_ft"),
            turn_rate_deg_sec=p.get("turn_rate_deg_sec"),
            current_heading=p.get("current_heading"),
            name=p.get("name"),
        )
        return {"found": found, "object": obj}

    def _ipc_set_simulator_state(self, p: Dict[str, Any]) -> Dict[str, Any]:
        running = bool(p.get("running", False))
        if running:
            self.simulator.start()
        else:
            self.simulator.stop()
        return {"status": "success", "running": self.simulator.is_running()}

    def start(self) -> None:
        """启动 IPC 管道监听"""
        if self.ch_webview:
            self.ch_webview.start()
        if self.ch_pilot:
            self.ch_pilot.start()
        if self.ch_master:
            self.ch_master.start()

    def stop(self) -> None:
        """优雅关闭服务"""
        logger.info("Stopping AirplaneSimulatorService...")
        self.simulator.stop()
        self._stop_event.set()
        if self.ch_webview:
            self.ch_webview.close()
        if self.ch_pilot:
            self.ch_pilot.close()
        if self.ch_master:
            self.ch_master.close()

    def run(self) -> None:
        """主进程执行入口"""
        logger.info(f"=== AirplaneSimulatorService started (PID: {os.getpid()}) ===")
        signal.signal(signal.SIGINT, lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        self.start()
        # 阻塞等待停止事件
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()
        logger.info(f"=== AirplaneSimulatorService exited (PID: {os.getpid()}) ===")


def simulator_process_entrypoint(
    conn_webview: Connection,
    conn_pilot: Connection,
    conn_master: Connection
) -> None:
    """供 multiprocessing.Process 拉起独立进程的函数入口"""
    service = AirplaneSimulatorService(
        conn_webview=conn_webview,
        conn_pilot=conn_pilot,
        conn_master=conn_master
    )
    service.run()


# 单例及向后兼容接口（保留供既有单进程测试使用）
simulator = AirplaneSimulator()

start = simulator.start
stop = simulator.stop
is_running = simulator.is_running
get_objects = simulator.get_objects
get_object = simulator.get_object
get_motion_state = simulator.get_motion_state
add_object = simulator.add_object
update_object = simulator.update_object
set_speed = simulator.set_speed
set_heading = simulator.set_heading
set_altitude = simulator.set_altitude
remove_object = simulator.remove_object
clear_objects = simulator.clear_objects
