"""
airplane_simulator.py

飞机运动学推算与状态管理模块（airplane_simulator 线程）。
负责所有关于航迹、航速、航向、高度及大圆航线运动学计算，
提供添加、修改、删除物体以及查询当前运动状态的接口。
"""

import copy
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log_dir = Path("../../logs")
log_dir.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("platform.airplane_simulator")

EARTH_RADIUS_M = 6_371_000
KNOT_TO_M_PER_S = 0.514444


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_heading(value, default: float = 0.0) -> float:
    heading = _to_float(value, default)
    if heading == 360.0:
        heading = 0.0
    return heading % 360.0


def _move_object(obj: Dict, dt: float) -> bool:
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
    飞机运动仿真器核心类。
    管理飞行器实体并独立在 airplane_simulator 线程中执行 0.5s 物理积分解算循环。
    """

    def __init__(self):
        self._objects: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """启动 airplane_simulator 仿真推算线程。若已在运行则返回 False。"""
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
            logger.info("airplane_simulator thread successfully started.")
            return True

    def stop(self) -> bool:
        """停止 airplane_simulator 仿真推算线程。若未运行则返回 False。"""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                logger.warning("airplane_simulator thread is not running.")
                return False
            self._stop_event.set()
            thread = self._thread

        thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
        logger.info("airplane_simulator thread successfully stopped.")
        return True

    def is_running(self) -> bool:
        """检查 airplane_simulator 线程是否正在运行。"""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """airplane_simulator 线程执行的运动推算主循环 (约 0.5s/tick)"""
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
    def get_objects(self) -> List[Dict]:
        """获取当前所有物体的实时运动状态快照。"""
        with self._lock:
            return [copy.deepcopy(obj) for obj in self._objects.values()]

    def get_object(self, object_id: str) -> Optional[Dict]:
        """根据 ID 获取单个物体的运动状态快照。"""
        with self._lock:
            obj = self._objects.get(object_id)
            return copy.deepcopy(obj) if obj else None

    def get_motion_state(self) -> List[Dict]:
        """供外部读取当前全量运动状态的统一接口（别名）。"""
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
    ) -> Dict:
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
        logger.info(f"Added object in simulator: id={object_id}, name={obj_name}, lat={new_obj['lat']}, lon={new_obj['lon']}")
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
    ) -> Tuple[bool, Optional[Dict]]:
        """
        修改指定物体的运动状态参数（速度、航向、高度等）。
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

            logger.info(f"Updated object {object_id} in simulator: speed={obj.get('speed_knots')}, target_heading={obj.get('target_heading')}, target_alt={obj.get('target_altitude_ft')}")
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
        with self._lock:
            if object_id in self._objects:
                self._objects.pop(object_id)
                logger.info(f"Removed object from simulator: id={object_id}")
                return True
            return False

    def clear_objects(self) -> None:
        """清空所有物体。"""
        with self._lock:
            self._objects.clear()


# 全局单例
simulator = AirplaneSimulator()

# 模块级便捷接口
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
