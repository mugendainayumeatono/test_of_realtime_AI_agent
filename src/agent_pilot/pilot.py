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
    ):
        self.conn_webview = conn_webview
        self.conn_simulator = conn_simulator
        self.conn_controller = conn_controller
        self.conn_master = conn_master

        self.ch_webview: Optional[IPCChannel] = None
        self.ch_simulator: Optional[IPCChannel] = None
        self.ch_controller: Optional[IPCChannel] = None
        self.ch_master: Optional[IPCChannel] = None

        self.prompt_file = prompt_file or PROMPT_FILE
        self._stop_event = asyncio.Event()

        self._setup_ipc()

    def _setup_ipc(self) -> None:
        if self.conn_webview:
            self.ch_webview = IPCChannel(self.conn_webview, name="pilot->webview")
            self.ch_webview.register_handler("radio_RX", self._ipc_handle_radio_rx)
            self.ch_webview.register_handler("test_ai", self._ipc_handle_test_ai)

        if self.conn_controller:
            self.ch_controller = IPCChannel(self.conn_controller, name="pilot->ctrl")
            self.ch_controller.register_handler("radio_RX", self._ipc_handle_radio_rx)

        if self.conn_simulator:
            self.ch_simulator = IPCChannel(self.conn_simulator, name="pilot->sim")
            self.ch_simulator.register_handler("radio_RX", self._ipc_handle_radio_rx)

        if self.conn_master:
            self.ch_master = IPCChannel(self.conn_master, name="pilot->master")
            self.ch_master.register_handler("ping", lambda p: {"status": "ok", "pid": os.getpid()})
            self.ch_master.register_handler("status", lambda p: {
                "status": "ok",
                "pid": os.getpid(),
                "running": not self._stop_event.is_set()
            })
            self.ch_master.register_handler("stop", lambda p: self.stop())
            self.ipc_log_handler = setup_child_logging(channel=self.ch_master, process_name="agent_pilot")
        else:
            self.ipc_log_handler = setup_child_logging(channel=None, process_name="agent_pilot")

    async def _ipc_handle_test_ai(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 通过管道发起的 AI 连通性测试请求"""
        logger.info("Received request to test AI connectivity on Pilot via IPC.")
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_pilot"
        return res

    async def _ipc_handle_radio_rx(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """响应 Webview 或 Controller 通过管道下发的无线电报文"""
        text = params.get("text", "")
        return await self.handle_radio_command(text)

    async def test_ai(self) -> Dict[str, Any]:
        """本地直接执行 AI 连通性测试"""
        res = await asyncio.to_thread(check_ai_connectivity)
        res["source"] = "agent_pilot"
        return res

    async def get_simulator_objects(self) -> List[Dict[str, Any]]:
        """从模拟器获取当前空域所有飞机对象（优先 IPC，回退 HTTP）"""
        if self.ch_simulator:
            try:
                return await self.ch_simulator.call("get_objects", timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to query objects from simulator via IPC: {e}")
                return []
        if self.ch_webview:
            try:
                return await self.ch_webview.call("get_objects", timeout=5.0)
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
        """向模拟器下达目标运动参数修改指令（优先 IPC，回退 HTTP）"""
        if self.ch_simulator:
            try:
                ipc_params = {
                    "id": object_id,
                    "speed": query_params.get("speed"),
                    "heading": query_params.get("heading"),
                    "altitude": query_params.get("altitude"),
                }
                res = await self.ch_simulator.call("update_object", ipc_params, timeout=5.0)
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
        """向模拟器或 Webview 推送机载解析日志（优先模拟器 IPC，回退 Webview IPC/HTTP）"""
        log_data = {
            "source": "agent_pilot",
            "type": "control_params",
            "data": params,
            "text": f"Target: {params.get('target')}, Speed: {params.get('speed')}, Heading: {params.get('heading')}, Alt: {params.get('altitude')}",
            "timestamp": time.time(),
        }
        if self.ch_webview:
            try:
                self.ch_webview.notify("add_radio_log", log_data)
                return
            except Exception as e:
                logger.error(f"Failed to send pilot radio log via Webview IPC: {e}")

        if self.ch_simulator:
            try:
                self.ch_simulator.notify("radio_log", log_data)
                return
            except Exception as e:
                logger.error(f"Failed to send pilot radio log via Simulator IPC: {e}")

        # 回退 HTTP
        async with httpx.AsyncClient() as client:
            try:
                await client.post(f"{PLATFORM_URL}/api/radio_log", json=log_data)
            except Exception as e:
                logger.error(f"Failed to post radio log via HTTP: {e}")

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

            # 调用模拟器更新每一架匹配的飞机
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

            # 推送解析后的参数至 Webview 界面无线电日志
            await self.post_radio_log(params)

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

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动 IPC 监听"""
        cur_loop = loop or asyncio.get_running_loop()
        if self.ch_webview:
            self.ch_webview.start(loop=cur_loop)
        if self.ch_controller:
            self.ch_controller.start(loop=cur_loop)
        if self.ch_simulator:
            self.ch_simulator.start(loop=cur_loop)
        if self.ch_master:
            self.ch_master.start(loop=cur_loop)

    def stop(self) -> None:
        """优雅关闭服务"""
        logger.info("Stopping Agent Pilot...")
        self._stop_event.set()
        if self.ch_webview:
            self.ch_webview.close()
        if self.ch_controller:
            self.ch_controller.close()
        if self.ch_simulator:
            self.ch_simulator.close()
        if self.ch_master:
            self.ch_master.close()
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
    conn_webview: Connection,
    conn_simulator: Connection,
    conn_controller: Connection,
    conn_master: Connection,
) -> None:
    """供 multiprocessing.Process 拉起独立进程的入口函数"""
    pilot_instance = AgentPilot(
        conn_webview=conn_webview,
        conn_simulator=conn_simulator,
        conn_controller=conn_controller,
        conn_master=conn_master,
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
