"""
src/agent_pilot/pilot.py

机载飞行员智能体 (Agent Pilot)。
彻底对象化设计：
1. AgentPilot: 核心飞行员类，负责无线电语音/指令语义解析、目标飞行器匹配、飞行物理参数下发与执行反馈。
2. 内部通信：重构为通过 multiprocessing.Pipe (IPCChannel) 接收来自 Webview 和 Controller 的 radio_RX 及 test_ai 请求，
   并通过管道调用 Simulator 执行飞机运动学参数更新、向 Webview 回传日志。
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

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 确保能引用 agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ai_client import (
    check_ai_connectivity,
    generate_ai_response,
    get_ai_model,
    test_ai_api,
)
from agent_common.ipc import IPCChannel, setup_child_logging, IPCLogHandler

logger = logging.getLogger("pilot")

load_dotenv()

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8010")
PROMPT_FILE = Path(__file__).parent / "prompt_template.txt"


class RadioCommand(BaseModel):
    text: str


class AgentPilot:
    """
    飞行员智能体业务对象。
    通过 IPC 管道接收无线电管制指令，利用大语言模型提取航速/航向/高度目标，并下达至飞行模拟器。
    """

    def __init__(
        self,
        conn_webview: Optional[Connection] = None,
        conn_simulator: Optional[Connection] = None,
        conn_controller: Optional[Connection] = None,
        conn_master: Optional[Connection] = None,
        prompt_file: Optional[Path] = None,
        conn_radio_simulator: Optional[Connection] = None,
        conn_flight_simulator: Optional[Connection] = None,
        conn_flight_master: Optional[Connection] = None,
        conn_flight_webview: Optional[Connection] = None,
    ):
        # 显式双队列连接；未提供时回退到旧单连接保持兼容
        self.conn_radio_simulator = conn_radio_simulator or conn_simulator
        self.conn_flight_simulator = conn_flight_simulator or conn_simulator
        self.conn_flight_master = conn_flight_master or conn_master
        self.conn_flight_webview = conn_flight_webview or conn_webview
        self.conn_controller = conn_controller

        # 兼容性属性别名
        self.conn_webview = self.conn_flight_webview
        self.conn_simulator = self.conn_flight_simulator
        self.conn_master = self.conn_flight_master

        # 独立 IPC 通道
        self.ch_radio_sim: Optional[IPCChannel] = None
        self.ch_flight_sim: Optional[IPCChannel] = None
        self.ch_flight_master: Optional[IPCChannel] = None
        self.ch_flight_webview: Optional[IPCChannel] = None
        self.ch_controller: Optional[IPCChannel] = None

        # 兼容性通道别名
        self.ch_simulator: Optional[IPCChannel] = None
        self.ch_webview: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None

        self.prompt_file = prompt_file or PROMPT_FILE
        self._stop_event = asyncio.Event()

        self._setup_ipc()

    def _setup_ipc(self) -> None:
        # 1. 飞行数据队列 (Flight Queue): 向模拟器查询物体与修改物理参数
        if self.conn_flight_simulator:
            self.ch_flight_sim = IPCChannel(self.conn_flight_simulator, name="pilot->sim:flight")
            self.ch_simulator = self.ch_flight_sim

        # 2. 无线电通信队列 (Radio Queue): 仅用于接收模拟器转发的无线电语音指令
        if self.conn_radio_simulator:
            if self.conn_radio_simulator is self.conn_flight_simulator and self.ch_flight_sim:
                self.ch_radio_sim = self.ch_flight_sim
                self.ch_radio_sim.register_handler("radio_RX", self._ipc_handle_radio_rx)
            else:
                self.ch_radio_sim = IPCChannel(self.conn_radio_simulator, name="pilot<-sim:radio")
                self.ch_radio_sim.register_handler("radio_RX", self._ipc_handle_radio_rx)

        # 3. 飞行/系统控制队列 (Flight/System Queue): Master 系统监控与常规日志
        if self.conn_flight_master:
            self.ch_flight_master = IPCChannel(self.conn_flight_master, name="pilot->master:flight")
            self.ch_master = self.ch_flight_master
            self.ch_flight_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_flight_master.register_handler("status", lambda p: {
                "status": "ok",
                "pid": os.getpid(),
                "running": not self._stop_event.is_set()
            })
            self.ch_flight_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_flight_master, process_name="agent_pilot")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="agent_pilot")

        # 4. 辅助队列: Webview (代理 AI 连通性测试) 与 Controller 兼容
        if self.conn_flight_webview:
            self.ch_flight_webview = IPCChannel(self.conn_flight_webview, name="pilot->webview:flight")
            self.ch_webview = self.ch_flight_webview
            self.ch_flight_webview.register_handler("test_ai", self._ipc_handle_test_ai)

        if self.conn_controller:
            self.ch_controller = IPCChannel(self.conn_controller, name="pilot->ctrl")
            self.ch_controller.register_handler("radio_RX", self._ipc_handle_radio_rx)

    async def _ipc_handle_test_ai(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 通过管道发起的 AI 连通性测试请求"""
        logger.info("Received request to test AI connectivity on Pilot via IPC.")
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_pilot"
        return res

    async def _ipc_handle_radio_rx(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应从无线电队列下发的无线电报文"""
        text = params.get("text", "")
        return await self.handle_radio_command(text)

    async def test_ai(self) -> Dict[str, Any]:
        """本地直接执行 AI 连通性测试"""
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_pilot"
        return res

    async def get_simulator_objects(self) -> List[Dict[str, Any]]:
        """从模拟器飞行数据队列获取当前空域所有飞机对象（优先 IPC，回退 HTTP）"""
        target_ch = self.ch_flight_sim or self.ch_simulator
        if target_ch:
            try:
                return await target_ch.call("get_objects", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to query objects from simulator via IPC: {e}")
                return []
        if self.ch_flight_webview or self.ch_webview:
            target_wv = self.ch_flight_webview or self.ch_webview
            try:
                return await target_wv.call("get_objects", timeout=5.0)
            except Exception:
                pass
        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{PLATFORM_URL}/api/objects")
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to query objects via HTTP: {e}")
                return []

    async def update_simulator_object(self, object_id: str, query_params: Dict[str, Any]) -> Dict[str, Any]:
        """向模拟器飞行数据队列下达目标运动参数修改指令（优先 IPC，回退 HTTP）"""
        target_ch = self.ch_flight_sim or self.ch_simulator
        if target_ch:
            try:
                ipc_params = {
                    "id": object_id,
                    "speed": query_params.get("speed"),
                    "heading": query_params.get("heading"),
                    "altitude": query_params.get("altitude"),
                }
                res = await target_ch.call("update_object", ipc_params, timeout=5.0)
                return {"status": "success", "object": res.get("object")}
            except Exception as e:
                logger.error(f"Failed to update object {object_id} via simulator IPC: {e}")
                return {"status": "error", "message": str(e)}

        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                params = {"id": object_id}
                params.update(query_params)
                resp = await client.get(f"{PLATFORM_URL}/api/update", params=params)
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to update object {object_id} via HTTP: {e}")
                return {"status": "error", "message": str(e)}

    async def post_radio_log(self, params: Dict[str, Any]) -> None:
        """机载解算日志（仅记录标准运行日志，不通过无线电管道或 IPC 发送）"""
        logger.info(f"Target: {params.get('target')}, Speed: {params.get('speed')}, Heading: {params.get('heading')}, Alt: {params.get('altitude')}")

    async def handle_radio_command(self, text: str) -> Dict[str, Any]:
        """
        核心自然语言指令解析与飞机参数解算。
        从语音指令中利用大模型抽取 target, speed, heading, altitude，并匹配目标飞机执行修改。
        """
        logger.info(f"Received radio command: {text}")
        if not get_ai_model():
            logger.error("API Key missing on radio_RX")
            return {"status": "error", "message": "Gemini/OpenAI API key not configured"}

        try:
            with open(self.prompt_file, "r", encoding="utf-8") as f:
                template = f.read()
        except Exception as e:
            return {"status": "error", "message": f"Could not read prompt template: {e}"}

        prompt = template.format(input_text=text)

        try:
            content = generate_ai_response(prompt)
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()

            params = json.loads(content)
            extracted_target = params.get("target")
            logger.info(f"Extracted parameters from Gemini: {params}")

            if not extracted_target:
                return {
                    "status": "error",
                    "message": "AI could not identify which object you are talking to.",
                    "extracted": params
                }

            # 匹配目标飞机
            all_objects = await self.get_simulator_objects()
            target_uuids = []
            target_clean = str(extracted_target).lower()

            for obj in all_objects:
                obj_id = str(obj.get("id", "")).lower()
                obj_name = str(obj.get("name", "")).lower()
                if target_clean == obj_id or target_clean == obj_name:
                    target_uuids.append(obj["id"])

            if not target_uuids:
                logger.warning(f"Could not find target object matching: {extracted_target}")
                return {
                    "status": "error",
                    "message": f"Could not find any active objects matching '{extracted_target}'",
                    "extracted": params
                }

            logger.info(f"Target '{extracted_target}' resolved to UUIDs: {target_uuids}")

            # 调用模拟器更新每一架匹配的飞机 (走飞行数据队列)
            results = []
            for uid in target_uuids:
                query_params: Dict[str, Any] = {}
                if params.get("speed") is not None:
                    query_params["speed"] = params["speed"]
                if params.get("heading") is not None:
                    query_params["heading"] = params["heading"]
                if params.get("altitude") is not None:
                    query_params["altitude"] = params["altitude"]

                update_resp = await self.update_simulator_object(uid, query_params)
                results.append({"id": uid, "response": update_resp})

            # 记录解算参数至标准日志（按需求不发送至无线电管道）
            logger.info(f"Target: {params.get('target')}, Speed: {params.get('speed')}, Heading: {params.get('heading')}, Alt: {params.get('altitude')}")

            return {
                "status": "success",
                "resolved_count": len(target_uuids),
                "resolved_ids": target_uuids,
                "extracted": params,
                "platform_responses": results
            }

        except Exception as e:
            logger.error(f"Error handling radio command: {e}")
            return {"status": "error", "message": str(e)}

    def _get_active_channels(self) -> List[IPCChannel]:
        """获取所有已初始化的唯一 IPCChannel 实例集合"""
        ch_set = set()
        for ch in [self.ch_radio_sim, self.ch_flight_sim, self.ch_flight_master, self.ch_flight_webview, self.ch_controller]:
            if ch is not None:
                ch_set.add(ch)
        return list(ch_set)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动所有 IPC 监听"""
        cur_loop = loop or asyncio.get_running_loop()
        for ch in self._get_active_channels():
            ch.start(loop=cur_loop)

    def stop(self) -> None:
        """优雅关闭服务"""
        logger.info("Stopping Agent Pilot...")
        self._stop_event.set()
        for ch in self._get_active_channels():
            ch.close()
        if hasattr(self, "ipc_log_handler") and self.ipc_log_handler:
            self.ipc_log_handler.set_channel(None)

    def run(self) -> None:
        """主进程执行入口（基于 asyncio 运行）"""
        logger.info(f"=== Agent Pilot Process started (PID: {os.getpid()}) ===")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        signal.signal(signal.SIGINT, lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        test_ai_api()
        self.start(loop=loop)
        try:
            loop.run_until_complete(self._stop_event.wait())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            logger.info(f"=== Agent Pilot Process exited (PID: {os.getpid()}) ===")
            self.stop()
            loop.close()


def pilot_process_entrypoint(
    conn_webview: Optional[Connection] = None,
    conn_simulator: Optional[Connection] = None,
    conn_controller: Optional[Connection] = None,
    conn_master: Optional[Connection] = None,
    conn_radio_simulator: Optional[Connection] = None,
    conn_flight_simulator: Optional[Connection] = None,
    conn_flight_master: Optional[Connection] = None,
    conn_flight_webview: Optional[Connection] = None,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的入口函数"""
    pilot_instance = AgentPilot(
        conn_webview=conn_webview,
        conn_simulator=conn_simulator,
        conn_controller=conn_controller,
        conn_master=conn_master,
        conn_radio_simulator=conn_radio_simulator,
        conn_flight_simulator=conn_flight_simulator,
        conn_flight_master=conn_flight_master,
        conn_flight_webview=conn_flight_webview,
    )
    pilot_instance.run()


# --- 全局单例与向后兼容接口（保留供测试桩直接调用） ---
default_pilot = AgentPilot()

app = FastAPI(title="Agent Pilot Standalone")

@app.get("/test_ai")
@app.post("/test_ai")
async def test_ai_endpoint():
    logger.info("Received request to test AI connectivity on Pilot.")
    res = await asyncio.to_thread(check_ai_connectivity)
    res["source"] = "agent_pilot"
    return res

@app.post("/radio_RX")
async def radio_rx(command: RadioCommand):
    res = await default_pilot.handle_radio_command(command.text)
    if res.get("status") == "error" and "configured" in res.get("message", ""):
        raise HTTPException(status_code=500, detail="Gemini API key not configured")
    return res

if __name__ == "__main__":
    default_pilot.run()
