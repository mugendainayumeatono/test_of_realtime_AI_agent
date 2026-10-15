"""
src/agent_controller/controller.py

进近空中交通管制员智能体 (Agent Controller)。
彻底对象化设计：
1. AgentController: 核心管制员类，负责空域态势感知、规则与大模型策略解算、冲突防范与无线电指令下发。
2. 内部通信：重构为通过 multiprocessing.Pipe (IPCChannel) 与 Webview 进程和 Pilot 进程通信，
   彻底废弃原有的内部 HTTP 协议。对外暴露 test_ai 管道接口供 Webview 代理调用。
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, TypeAdapter

# 确保能引用 agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ai_client import (
    check_ai_connectivity,
    generate_ai_response,
    get_ai_model,
    test_ai_api,
)
from agent_common.ipc import IPCChannel, setup_child_logging, IPCLogHandler
from agent_common.radio import IPCRadio, DEFAULT_RADIO_CHANNEL

logger = logging.getLogger("controller")

load_dotenv()

PROMPT_FILE = Path(__file__).parent / "prompt_template.txt"


class ScheduleItem(BaseModel):
    """大模型结构化输出的单机调度项"""
    id: str = Field(
        description="Aircraft callsign or unique identifier matching the target in the airspace (e.g., 'JA-NRT-4000')."
    )
    is_schedule: bool = Field(
        description="Whether tactical ATC scheduling instruction is required for this aircraft. True if heading, altitude, speed adjustment or approach/landing clearance is needed; False if maintaining current flight parameters is sufficient."
    )
    msg: str = Field(
        default="",
        description="The exact ATC natural language voice instruction to be transmitted via radio when is_schedule is True. Must explicitly include the aircraft callsign. Must be empty string when is_schedule is False."
    )


class AgentController:
    """
    管制员智能体业务对象。
    通过管道与 Webview 和 Pilot 通信，执行空域监测、大模型决策与无线电调度。
    """

    def __init__(
        self,
        conn_flight_webview: Optional[Connection] = None,
        conn_flight_simulator: Optional[Connection] = None,
        conn_radio_simulator: Optional[Connection] = None,
        conn_flight_master: Optional[Connection] = None,
        prompt_file: Optional[Path] = None,
        poll_interval: float = 10.0,
    ):
        self.conn_flight_webview = conn_flight_webview
        self.conn_flight_simulator = conn_flight_simulator
        self.conn_radio_simulator = conn_radio_simulator
        self.conn_flight_master = conn_flight_master

        # 无线电通信广播类
        self.radio: Optional[IPCRadio] = None
        if self.conn_radio_simulator:
            self.radio = IPCRadio(
                endpoint_id="agent_controller",
                connection=self.conn_radio_simulator,
            )

        # 独立 IPC 通道
        self.ch_radio_sim: Optional[IPCChannel] = self.radio.ipc_channel if self.radio else None
        self.ch_flight_sim: Optional[IPCChannel] = None
        self.ch_flight_master: Optional[IPCChannel] = None
        self.ch_flight_webview: Optional[IPCChannel] = None

        self.prompt_file = prompt_file or PROMPT_FILE
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None

        # 管制员使能开关状态：初始化为 disable 状态 (False)
        self.enabled: bool = False
        logger.info("Initialized controller_state to enabled=False on startup (initial clean airspace).")

        self._setup_ipc()

    def _setup_ipc(self) -> None:
        # 1. 飞行数据队列 (Flight Queue): 向模拟器查询物体或配置
        if self.conn_flight_simulator:
            self.ch_flight_sim = IPCChannel(self.conn_flight_simulator, name="ctrl->sim:flight")

        # 2. 飞行/系统控制队列 (Flight/System Queue): Master 集中日志与监控
        if self.conn_flight_master:
            self.ch_flight_master = IPCChannel(self.conn_flight_master, name="ctrl->master:flight")
            self.ch_flight_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_flight_master.register_handler("status", lambda p: {
                "status": "ok",
                "pid": os.getpid(),
                "running": not self._stop_event.is_set()
            })
            self.ch_flight_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_flight_master, process_name="agent_controller")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="agent_controller")

        # 3. Webview 管道: AI 连通性测试代理与状态查询/控制
        if self.conn_flight_webview:
            self.ch_flight_webview = IPCChannel(self.conn_flight_webview, name="ctrl->webview:flight")
            self.ch_flight_webview.register_handler("test_ai", self._ipc_handle_test_ai)
            self.ch_flight_webview.register_handler("get_controller_state", self._ipc_handle_get_controller_state)
            self.ch_flight_webview.register_handler("set_controller_state", self._ipc_handle_set_controller_state)

    async def _ipc_handle_get_controller_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 通过管道发起的管制员使能状态查询请求"""
        return {"enabled": self.enabled}

    async def _ipc_handle_set_controller_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 通过管道发起的管制员使能状态设置请求"""
        enabled = bool(params.get("enabled", False))
        self.set_controller_state(enabled)
        return {"status": "success", "enabled": self.enabled}

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
        """获取机场配置（通过飞行数据队列 IPC 向模拟器查询）"""
        if self.ch_flight_sim:
            try:
                return await self.ch_flight_sim.call("get_airport", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to fetch airport via IPC: {e}")
        return {"lat": None, "lon": None}

    async def get_objects(self) -> List[Dict[str, Any]]:
        """获取空域所有物体（通过飞行数据队列 IPC 向模拟器查询）"""
        if self.ch_flight_sim:
            try:
                return await self.ch_flight_sim.call("get_objects", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to fetch objects via IPC: {e}")
        return []

    async def get_controller_state(self) -> Dict[str, Any]:
        """获取管制员使能开关状态（读取自身状态）"""
        return {"enabled": self.enabled}

    def set_controller_state(self, enabled: bool) -> None:
        """设置管制员使能开关状态"""
        self.enabled = bool(enabled)
        logger.info(f"Controller state set to enabled={self.enabled}")

    async def send_radio_message(self, text: str, channel: str = DEFAULT_RADIO_CHANNEL) -> Any:
        """通过无线电广播类下发管制无线电指令至对应频道（发送前记录日志并广播）"""
        logger.info(f"Controller issuing command on channel [{channel}]: {text}")
        payload = {
            "source": "agent_controller",
            "type": "voice",
            "text": text,
            "timestamp": time.time(),
        }

        # 优先使用 IPCRadio 异步收发类（发送前自动记录日志并广播）
        if self.radio:
            try:
                return await self.radio.send(channel_name=channel, data=payload)
            except Exception as e:
                logger.error(f"Failed to send radio message via IPCRadio: {e}")
        return None

    @staticmethod
    def parse_schedule_response(text: str) -> List[ScheduleItem]:
        """
        解析大模型输出的 JSON 格式调度指令数组。
        使用 Pydantic TypeAdapter 严格校验，若非 JSON 或解析校验失败则记录错误日志并返回空列表。
        """
        if not text:
            return []
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if not cleaned:
            return []

        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                data = [data]
            adapter = TypeAdapter(List[ScheduleItem])
            return adapter.validate_python(data)
        except Exception as e:
            logger.error(f"Failed to parse AI schedule response as JSON: {e}. Raw response: {text}")
            return []

    async def run_loop(self) -> None:
        """管制员空域态势感知与大模型决策主循环"""
        if not get_ai_model():
            logger.error("AI Model API key not configured. Exiting loop.")
            return

        try:
            await asyncio.to_thread(test_ai_api)
        except Exception as e:
            logger.warning(f"Initial AI API connectivity test failed: {e}")
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

                # 构造上下文数据并拼接到提示词最后（仅传入空中活动飞机，不包含地面飞机）
                airspace_context = {
                    "airport": airport,
                    "aircraft": airborne_objects,
                }
                prompt = f"{template.strip()}\n\n{json.dumps(airspace_context, ensure_ascii=False, indent=2)}"

                # 通过 response_schema 与 Pydantic 引导大模型输出结构化调度数组 (在线程池中异步执行，不阻塞事件循环与 IPC 响应)
                text = await asyncio.to_thread(generate_ai_response, prompt, List[ScheduleItem])

                # 校验等待 AI 期间管制员使能状态是否已被关闭
                c_state = await self.get_controller_state()
                if not c_state.get("enabled", False):
                    logger.info("Controller state was disabled while AI response was generating. Discarding dispatch.")
                    await asyncio.sleep(self.poll_interval)
                    continue

                schedule_items = self.parse_schedule_response(text)

                # 遍历大模型输出的数组，收集需要调度的指令
                scheduled_msgs = []
                for item in schedule_items:
                    if item.is_schedule:
                        if item.msg and item.msg.strip():
                            scheduled_msgs.append(item.msg.strip())
                    else:
                        logger.info(f"AI determined {item.id} does not need flight state changes (monitoring only).")

                # 把需要调度的指令全部拼接起来，然后走既存的无线电通信逻辑
                if scheduled_msgs:
                    combined_msg = ", ".join(scheduled_msgs)
                    await self.send_radio_message(combined_msg)

            except Exception as e:
                logger.error(f"Error in controller loop: {e}")

            await asyncio.sleep(self.poll_interval)

    def _get_active_channels(self) -> List[IPCChannel]:
        """获取所有已初始化的唯一 IPCChannel 实例集合"""
        ch_set = set()
        for ch in [self.ch_radio_sim, self.ch_flight_sim, self.ch_flight_master, self.ch_flight_webview]:
            if ch is not None:
                ch_set.add(ch)
        return list(ch_set)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动 IPC 监听与调度任务"""
        cur_loop = loop or asyncio.get_running_loop()
        if self.radio:
            self.radio.start(loop=cur_loop)
            self.radio.create_channel(DEFAULT_RADIO_CHANNEL)
            self.radio.register(DEFAULT_RADIO_CHANNEL)
        for ch in self._get_active_channels():
            ch.start(loop=cur_loop)
        self._loop_task = cur_loop.create_task(self.run_loop())

    def stop(self) -> None:
        """优雅停止管制员"""
        logger.info("Stopping Agent Controller...")
        self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
        if self.radio:
            self.radio.close()
        for ch in self._get_active_channels():
            ch.close()
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
    conn_radio_simulator: Optional[Connection] = None,
    conn_flight_simulator: Optional[Connection] = None,
    conn_flight_master: Optional[Connection] = None,
    conn_flight_webview: Optional[Connection] = None,
    **kwargs,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的入口函数"""
    try:
        controller_instance = AgentController(
            conn_radio_simulator=conn_radio_simulator,
            conn_flight_simulator=conn_flight_simulator,
            conn_flight_master=conn_flight_master,
            conn_flight_webview=conn_flight_webview,
        )
        controller_instance.run()
    except Exception as e:
        import traceback
        logger.critical(f"Fatal error during agent_controller startup: {e}", exc_info=True)
        sys.stderr.write(f"Fatal error in agent_controller:\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        raise



if __name__ == "__main__":
    controller_process_entrypoint()
