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
from agent_common.ipc import IPCChannel, setup_child_logging, IPCLogHandler

logger = logging.getLogger("platform.airplane_simulator")

EARTH_RADIUS_M = 6_371_000
KNOT_TO_M_PER_S = 0.514444
NAUTICAL_MILE_M = 1852.0


def calculate_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两经纬度点之间的球面大圆距离（米）"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c


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
        self._airport: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_airport(
        self,
        lat: Optional[float],
        lon: Optional[float],
        runway_heading: Optional[float] = None,
    ) -> None:
        """设置机场地理坐标与跑道方向，并同步刷新当前所有物体的状态。"""
        with self._lock:
            if lat is None or lon is None:
                self._airport = None
            else:
                self._airport = {
                    "lat": _to_float(lat),
                    "lon": _to_float(lon),
                    "runway_heading": _to_float(runway_heading) if runway_heading is not None else None,
                }
            for obj in self._objects.values():
                self._update_object_status(obj)
        logger.info(f"Simulator airport set to: {self._airport}")

    def get_airport(self) -> Optional[Dict[str, Any]]:
        """获取当前配置的机场地理坐标。"""
        with self._lock:
            return copy.deepcopy(self._airport) if self._airport else None

    def _evaluate_status(self, obj: Dict[str, Any]) -> str:
        """
        根据当前与机场的距离和高度计算状态：
        接近机场 1 海里内 (<= 1852.0 m) 且高度低于 100 英尺 (< 100.0 ft) 时为 ground，
        离开该范围则为 airborne。若未设置机场，则默认 airborne。
        """
        if not self._airport:
            return "airborne"
        airport_lat = self._airport.get("lat")
        airport_lon = self._airport.get("lon")
        if airport_lat is None or airport_lon is None:
            return "airborne"

        lat = _to_float(obj.get("lat", 0.0))
        lon = _to_float(obj.get("lon", 0.0))
        dist_m = calculate_distance_m(lat, lon, _to_float(airport_lat), _to_float(airport_lon))
        alt_ft = _to_float(obj.get("altitude_ft", 0.0))

        if dist_m <= NAUTICAL_MILE_M and alt_ft < 100.0:
            return "ground"
        return "airborne"

    def _update_object_status(self, obj: Dict[str, Any]) -> None:
        """
        更新物体状态。当状态由 airborne 变为 ground 时，
        自动将飞机的航速和高度变化率设为 0。
        """
        new_status = self._evaluate_status(obj)
        prev_status = obj.get("status", "airborne")

        if prev_status == "airborne" and new_status == "ground":
            obj["speed_knots"] = 0.0
            obj["vertical_speed_ft_min"] = 0.0
            obj["target_altitude_ft"] = obj.get("altitude_ft", 0.0)
            logger.info(
                f"Aircraft {obj.get('name', obj.get('id'))} touched down (status -> ground). "
                f"Speed and vertical speed set to 0."
            )
        elif prev_status == "ground" and new_status == "airborne":
            logger.info(
                f"Aircraft {obj.get('name', obj.get('id'))} became airborne (status -> airborne)."
            )

        obj["status"] = new_status

    def step(self, dt: float = 0.5) -> None:
        """执行单步物理积分解算与状态更新（可供调试与测试使用）。"""
        with self._lock:
            for obj in self._objects.values():
                _move_object(obj, dt)
                self._update_object_status(obj)

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

            self.step(dt)
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
            "status": "airborne",
        }
        with self._lock:
            self._update_object_status(new_obj)
            self._objects[object_id] = new_obj
        logger.info(f"Added object: id={object_id}, name={obj_name}, lat={new_obj['lat']}, lon={new_obj['lon']}, status={new_obj['status']}")
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

            self._update_object_status(obj)
            logger.info(f"Updated object {object_id}: status={obj.get('status')}, speed={obj.get('speed_knots')}, target_heading={obj.get('target_heading')}, target_alt={obj.get('target_altitude_ft')}")
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

    def restore_objects(self, objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        清空现有物体，并从传入的物体列表批量恢复/还原实体及其全套飞行状态参数。
        返回还原后的物体列表快照。
        """
        with self._lock:
            self._objects.clear()
            for item in (objects or []):
                obj_id = str(item.get("id") or uuid.uuid4())
                name = item.get("name") or f"object-{obj_id[:8]}"
                obj = {
                    "id": obj_id,
                    "name": name,
                    "lat": _to_float(item.get("lat"), 0.0),
                    "lon": _to_float(item.get("lon"), 0.0),
                    "heading": _normalize_heading(item.get("heading"), 360.0),
                    "speed_knots": _to_float(item.get("speed_knots", item.get("speed")), 0.0),
                    "altitude_ft": _to_float(item.get("altitude_ft", item.get("altitude")), 0.0),
                    "vertical_speed_ft_min": _to_float(item.get("vertical_speed_ft_min"), 0.0),
                    "turn_rate_deg_sec": _to_float(item.get("turn_rate_deg_sec"), 3.0),
                    "target_heading": _normalize_heading(item.get("target_heading")) if item.get("target_heading") is not None else _normalize_heading(item.get("heading"), 360.0),
                    "target_altitude_ft": _to_float(item.get("target_altitude_ft")) if item.get("target_altitude_ft") is not None else _to_float(item.get("altitude_ft", item.get("altitude")), 0.0),
                    "status": item.get("status", "airborne"),
                }
                self._update_object_status(obj)
                self._objects[obj_id] = obj
            logger.info(f"Restored {len(self._objects)} objects into simulator.")
            return [copy.deepcopy(o) for o in self._objects.values()]


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
        conn_controller: Optional[Connection] = None,
        conn_radio_controller: Optional[Connection] = None,
        conn_radio_pilot: Optional[Connection] = None,
        conn_radio_webview: Optional[Connection] = None,
        conn_radio_master: Optional[Connection] = None,
        conn_flight_controller: Optional[Connection] = None,
        conn_flight_pilot: Optional[Connection] = None,
        conn_flight_webview: Optional[Connection] = None,
        conn_flight_master: Optional[Connection] = None,
    ):
        self.simulator = AirplaneSimulator()
        
        # 显式两套队列连接；未提供时平滑回退到旧单连接
        self._conn_radio_ctrl = conn_radio_controller or conn_controller
        self._conn_radio_pilot = conn_radio_pilot or conn_pilot
        self._conn_radio_webview = conn_radio_webview or conn_webview
        self._conn_radio_master = conn_radio_master or conn_master

        self._conn_flight_ctrl = conn_flight_controller or conn_controller
        self._conn_flight_pilot = conn_flight_pilot or conn_pilot
        self._conn_flight_webview = conn_flight_webview or conn_webview
        self._conn_flight_master = conn_flight_master or conn_master

        # 独立 IPC 通道
        self.ch_radio_ctrl: Optional[IPCChannel] = None
        self.ch_radio_pilot: Optional[IPCChannel] = None
        self.ch_radio_webview: Optional[IPCChannel] = None
        self.ch_radio_master: Optional[IPCChannel] = None

        self.ch_flight_ctrl: Optional[IPCChannel] = None
        self.ch_flight_pilot: Optional[IPCChannel] = None
        self.ch_flight_webview: Optional[IPCChannel] = None
        self.ch_flight_master: Optional[IPCChannel] = None

        # 兼容性通道别名
        self.ch_webview: Optional[IPCChannel] = None
        self.ch_pilot: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None
        self.ch_controller: Optional[IPCChannel] = None

        self._stop_event = threading.Event()
        self._setup_channels()

    def _setup_channels(self) -> None:
        # --- 1. 飞行数据与控制队列 (Flight Queue) ---
        if self._conn_flight_webview:
            self.ch_flight_webview = IPCChannel(self._conn_flight_webview, name="sim<-webview:flight")
            self._register_flight_handlers(self.ch_flight_webview)
            self.ch_webview = self.ch_flight_webview

        if self._conn_flight_pilot:
            self.ch_flight_pilot = IPCChannel(self._conn_flight_pilot, name="sim<-pilot:flight")
            self._register_flight_handlers(self.ch_flight_pilot)
            self.ch_pilot = self.ch_flight_pilot

        if self._conn_flight_ctrl:
            self.ch_flight_ctrl = IPCChannel(self._conn_flight_ctrl, name="sim<-ctrl:flight")
            self._register_flight_handlers(self.ch_flight_ctrl)
            self.ch_controller = self.ch_flight_ctrl

        if self._conn_flight_master:
            self.ch_flight_master = IPCChannel(self._conn_flight_master, name="sim->master:flight")
            self.ch_master = self.ch_flight_master
            self.ch_flight_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_flight_master.register_handler("status", lambda p: {
                "status": "ok",
                "running": self.simulator.is_running(),
                "pid": os.getpid(),
                "objects_count": len(self.simulator.get_objects())
            })
            self.ch_flight_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_flight_master, process_name="airplane_simulator")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="airplane_simulator")

        # --- 2. 无线电通信队列 (Radio Queue) ---
        # 模拟器作为无线电核心中转站：从 ctrl 接收 -> 转给 pilot / webview / master
        if self._conn_radio_ctrl:
            if self._conn_radio_ctrl is self._conn_flight_ctrl and self.ch_flight_ctrl:
                self.ch_radio_ctrl = self.ch_flight_ctrl
            else:
                self.ch_radio_ctrl = IPCChannel(self._conn_radio_ctrl, name="sim<-ctrl:radio")
            self.ch_radio_ctrl.register_handler("radio_transmit", self._ipc_handle_radio_transmit)
            self.ch_radio_ctrl.register_handler("radio_message", self._ipc_handle_radio_transmit)

        if self._conn_radio_pilot:
            if self._conn_radio_pilot is self._conn_flight_pilot and self.ch_flight_pilot:
                self.ch_radio_pilot = self.ch_flight_pilot
            else:
                self.ch_radio_pilot = IPCChannel(self._conn_radio_pilot, name="sim->pilot:radio")

        if self._conn_radio_webview:
            if self._conn_radio_webview is self._conn_flight_webview and self.ch_flight_webview:
                self.ch_radio_webview = self.ch_flight_webview
            else:
                self.ch_radio_webview = IPCChannel(self._conn_radio_webview, name="sim->webview:radio")

        if self._conn_radio_master:
            if self._conn_radio_master is self._conn_flight_master and self.ch_flight_master:
                self.ch_radio_master = self.ch_flight_master
            else:
                self.ch_radio_master = IPCChannel(self._conn_radio_master, name="sim->master:radio")

    def _register_flight_handlers(self, channel: IPCChannel) -> None:
        """向飞行数据 IPC 通道注册模拟器物体与环境操作命令（不包含无线电指令）"""
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
        channel.register_handler("restore_objects", lambda p: self.simulator.restore_objects(
            p.get("objects", p) if isinstance(p, dict) else (p if isinstance(p, list) else [])
        ))
        channel.register_handler("start_simulator", lambda p: {"status": "success", "running": self.simulator.start() or self.simulator.is_running()})
        channel.register_handler("stop_simulator", lambda p: {"status": "success", "running": not self.simulator.stop() and self.simulator.is_running()})
        channel.register_handler("get_simulator_state", lambda p: {"running": self.simulator.is_running()})
        channel.register_handler("set_simulator_state", lambda p: self._ipc_set_simulator_state(p))
        channel.register_handler("set_speed", lambda p: self.simulator.set_speed(p["id"], _to_float(p["speed_knots"])))
        channel.register_handler("set_heading", lambda p: self.simulator.set_heading(p["id"], _to_float(p["heading"]), p.get("as_target", True)))
        channel.register_handler("set_altitude", lambda p: self.simulator.set_altitude(p["id"], _to_float(p["altitude_ft"]), p.get("as_target", True)))
        channel.register_handler("set_airport", lambda p: (self.simulator.set_airport(
            lat=p.get("lat"),
            lon=p.get("lon"),
            runway_heading=p.get("runway_heading"),
        ), True)[1])
        channel.register_handler("get_airport", lambda p: self.simulator.get_airport())

    def _register_simulator_handlers(self, channel: IPCChannel) -> None:
        """兼容旧测试桩：注册全量命令"""
        self._register_flight_handlers(channel)
        channel.register_handler("radio_transmit", self._ipc_handle_radio_transmit)
        channel.register_handler("radio_message", self._ipc_handle_radio_transmit)
        channel.register_handler("radio_log", self._ipc_handle_radio_log)

    def _ipc_handle_radio_transmit(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理管制员（或其他进程）发来的无线电通信内容：
        1. 发送给 Master 进程无线电队列记录到独立轮转日志中 (Requirement 3)
        2. 推送给 Webview 进程无线电队列更新网页内容与提供 CLI 查看 (Requirement 5 & 6)
        3. 转发给 Pilot 进程无线电队列执行 (Requirement 2)
        """
        text = p.get("text", "")
        source = p.get("source", "agent_controller")
        msg_type = p.get("type", "voice")
        ts = p.get("timestamp") or time.time()
        logger.info(f"Simulator received radio transmission from [{source}]: '{text}'")

        radio_payload = {
            "text": text,
            "source": source,
            "type": msg_type,
            "data": p.get("data"),
            "timestamp": ts,
        }

        # 1. 记录到 Master 进程的独立无线电轮转日志 (优先无线电专线)
        target_master = self.ch_radio_master or self.ch_master
        if target_master:
            try:
                target_master.notify("radio_log", radio_payload)
            except Exception as e:
                logger.error(f"Failed to relay radio log to master radio queue via IPC: {e}")

        # 2. 推送给 Webview 进程无线电专线
        target_wv = self.ch_radio_webview or self.ch_webview
        if target_wv:
            try:
                target_wv.notify("add_radio_log", radio_payload)
            except Exception as e:
                logger.error(f"Failed to relay radio log to webview radio queue via IPC: {e}")

        # 3. 转发给 Pilot 进程无线电专线
        pilot_resp = None
        relayed = False
        target_pilot = self.ch_radio_pilot or self.ch_pilot
        if target_pilot:
            try:
                pilot_resp = target_pilot.call_sync("radio_RX", {"text": text, "source": source}, timeout=30.0)
                logger.info(f"Simulator forwarded radio to pilot, response: {pilot_resp}")
                relayed = True
            except Exception as e:
                logger.error(f"Failed to forward radio message to pilot via IPC: {e}")
                pilot_resp = {"status": "error", "message": str(e)}
                relayed = False
        else:
            pilot_resp = {"status": "error", "message": "Pilot IPC channel not available"}
            relayed = False

        status = "success" if (relayed and isinstance(pilot_resp, dict) and pilot_resp.get("status") != "error") else ("success" if relayed else "error")

        return {
            "status": status,
            "relayed": relayed,
            "pilot_response": pilot_resp,
        }

    def _ipc_handle_radio_log(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """处理来自飞行员或其他来源的无线电日志回传"""
        ts = p.get("timestamp") or time.time()
        p["timestamp"] = ts
        target_master = self.ch_radio_master or self.ch_master
        if target_master:
            try:
                target_master.notify("radio_log", p)
            except Exception as e:
                logger.error(f"Failed to relay radio log to master via IPC: {e}")
        target_wv = self.ch_radio_webview or self.ch_webview
        if target_wv:
            try:
                target_wv.notify("add_radio_log", p)
            except Exception as e:
                logger.error(f"Failed to relay radio log to webview via IPC: {e}")
        return {"status": "success"}

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

    def _get_active_channels(self) -> List[IPCChannel]:
        """获取所有唯一已初始化的 IPCChannel 实例集合"""
        ch_set = set()
        for ch in [
            self.ch_radio_ctrl, self.ch_radio_pilot, self.ch_radio_webview, self.ch_radio_master,
            self.ch_flight_ctrl, self.ch_flight_pilot, self.ch_flight_webview, self.ch_flight_master
        ]:
            if ch is not None:
                ch_set.add(ch)
        return list(ch_set)

    def start(self) -> None:
        """启动 IPC 管道监听（模拟器服务为同步线程，无需事件循环）"""
        for ch in self._get_active_channels():
            ch.start(loop=False)

    def stop(self) -> None:
        """优雅关闭服务"""
        logger.info("Stopping AirplaneSimulatorService...")
        self.simulator.stop()
        self._stop_event.set()
        for ch in self._get_active_channels():
            ch.close()
        if hasattr(self, "ipc_log_handler") and self.ipc_log_handler:
            self.ipc_log_handler.set_channel(None)

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
            pass
        finally:
            logger.info(f"=== AirplaneSimulatorService exited (PID: {os.getpid()}) ===")
            self.stop()


def simulator_process_entrypoint(
    conn_webview: Optional[Connection] = None,
    conn_pilot: Optional[Connection] = None,
    conn_master: Optional[Connection] = None,
    conn_controller: Optional[Connection] = None,
    conn_radio_controller: Optional[Connection] = None,
    conn_radio_pilot: Optional[Connection] = None,
    conn_radio_webview: Optional[Connection] = None,
    conn_radio_master: Optional[Connection] = None,
    conn_flight_controller: Optional[Connection] = None,
    conn_flight_pilot: Optional[Connection] = None,
    conn_flight_webview: Optional[Connection] = None,
    conn_flight_master: Optional[Connection] = None,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的函数入口"""
    service = AirplaneSimulatorService(
        conn_webview=conn_webview,
        conn_pilot=conn_pilot,
        conn_master=conn_master,
        conn_controller=conn_controller,
        conn_radio_controller=conn_radio_controller,
        conn_radio_pilot=conn_radio_pilot,
        conn_radio_webview=conn_radio_webview,
        conn_radio_master=conn_radio_master,
        conn_flight_controller=conn_flight_controller,
        conn_flight_pilot=conn_flight_pilot,
        conn_flight_webview=conn_flight_webview,
        conn_flight_master=conn_flight_master,
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
restore_objects = simulator.restore_objects
set_airport = simulator.set_airport
get_airport = simulator.get_airport
step = simulator.step
