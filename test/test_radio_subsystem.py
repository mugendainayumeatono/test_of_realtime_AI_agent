"""
test/test_radio_subsystem.py

Unit tests for the refactored radio communication subsystem:
1. Controller -> Simulator -> Pilot radio relay (decoupled direct controller-pilot IPC).
2. Simulator broadcast of radio logs to Master (centralized radio.log with RotatingFileHandler)
   and Webview (WebSocket broadcast + buffer).
3. Webview HTTP API (GET /api/radio_log with pagination) and WebSocket buffer.
4. CLI radio commands and API query verification (strictly via Webview HTTP endpoint).
"""

import asyncio
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add cli, src, and src/platform to sys.path
CLI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cli"))
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SIM_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "platform"))

if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if SIM_DIR not in sys.path:
    sys.path.insert(0, SIM_DIR)

from fastapi.testclient import TestClient

from agent_common.ipc import IPCChannel
from airplane_simulator import AirplaneSimulatorService
from webview import WebviewApp
from agent_controller.controller import AgentController
from agent_pilot.pilot import AgentPilot
from master import MasterProcess
import cli as cli_module


class TestRadioRelaySubsystem(unittest.IsolatedAsyncioTestCase):
    """Verifies that Controller sends radio commands through Simulator, which relays to Pilot."""

    async def asyncSetUp(self):
        # Create duplex pipes
        # 1. Controller <-> Simulator
        self.p_ctrl_sim_c, self.p_ctrl_sim_s = multiprocessing.Pipe(duplex=True)
        # 2. Simulator <-> Pilot
        self.p_sim_pilot_s, self.p_sim_pilot_p = multiprocessing.Pipe(duplex=True)
        # 3. Simulator <-> Master
        self.p_sim_master_s, self.p_sim_master_m = multiprocessing.Pipe(duplex=True)
        # 4. Simulator <-> Webview
        self.p_sim_wv_s, self.p_sim_wv_w = multiprocessing.Pipe(duplex=True)

        # Simulator service
        self.sim_service = AirplaneSimulatorService(
            conn_webview=self.p_sim_wv_s,
            conn_pilot=self.p_sim_pilot_s,
            conn_master=self.p_sim_master_s,
            conn_controller=self.p_ctrl_sim_s,
        )

        # Peer channels to simulate endpoints
        self.ch_pilot = IPCChannel(self.p_sim_pilot_p, name="peer_pilot")
        self.ch_master = IPCChannel(self.p_sim_master_m, name="peer_master")
        self.ch_webview = IPCChannel(self.p_sim_wv_w, name="peer_webview")

        self.ch_pilot.start(loop=False)
        self.ch_master.start(loop=False)
        self.ch_webview.start(loop=False)
        self.sim_service.start()

        # Controller
        self.controller = AgentController(
            conn_webview=None,
            conn_pilot=None,
            conn_master=None,
            conn_simulator=self.p_ctrl_sim_c,
        )
        loop = asyncio.get_running_loop()
        self.controller.ch_simulator.start(loop=loop)

    async def asyncTearDown(self):
        self.controller.ch_simulator.close()
        self.ch_pilot.close()
        self.ch_master.close()
        self.ch_webview.close()
        self.sim_service.stop()

    async def test_controller_relays_radio_via_simulator_to_pilot_and_logs(self):
        """Controller.send_radio_message -> Simulator.radio_transmit -> Pilot.radio_RX, Master.radio_log, Webview.add_radio_log."""
        pilot_received = []
        master_logs = []
        webview_logs = []

        # Register handlers on peer channels
        def handle_pilot_rx(params):
            pilot_received.append(params)
            return {"status": "success", "resolved_count": 1, "action": "executed"}

        def handle_master_log(params):
            master_logs.append(params)

        def handle_webview_log(params):
            webview_logs.append(params)

        self.ch_pilot.register_handler("radio_RX", handle_pilot_rx)
        self.ch_master.register_handler("radio_log", handle_master_log)
        self.ch_webview.register_handler("add_radio_log", handle_webview_log)

        # Controller sends radio command
        test_msg = "Drone-42 climb to 9000, speed 220"
        res = await self.controller.send_radio_message(test_msg)

        # Wait briefly for async notify propagation
        await asyncio.sleep(0.1)

        # 1. Verify response from controller method
        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("relayed"))
        self.assertEqual(res.get("pilot_response", {}).get("action"), "executed")

        # 2. Verify pilot received the radio RX instruction
        self.assertEqual(len(pilot_received), 1)
        self.assertEqual(pilot_received[0]["text"], test_msg)
        self.assertEqual(pilot_received[0]["source"], "agent_controller")

        # 3. Verify master received radio log
        self.assertEqual(len(master_logs), 1)
        self.assertEqual(master_logs[0]["text"], test_msg)
        self.assertEqual(master_logs[0]["source"], "agent_controller")

        # 4. Verify webview received radio log
        self.assertEqual(len(webview_logs), 1)
        self.assertEqual(webview_logs[0]["text"], test_msg)
        self.assertEqual(webview_logs[0]["source"], "agent_controller")

    async def test_simulator_reports_failure_when_pilot_disconnected(self):
        """When pilot channel is not available, simulator returns status=error, relayed=False."""
        sim_without_pilot = AirplaneSimulatorService(
            conn_webview=None,
            conn_pilot=None,
            conn_master=None,
            conn_controller=None,
        )
        res = sim_without_pilot._ipc_handle_radio_transmit({"text": "Test drone climb", "source": "agent_controller"})
        self.assertEqual(res.get("status"), "error")
        self.assertFalse(res.get("relayed"))
        self.assertIn("error", res.get("pilot_response", {}).get("status"))


class TestMasterCentralizedRadioLogging(unittest.TestCase):
    """Verifies that Master creates and writes to a dedicated radio.log using RotatingFileHandler."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)
        self.master_log_file = self.log_dir / "master.log"
        self.radio_log_file = self.log_dir / "radio.log"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_master_radio_log_file_and_rotation(self):
        master = MasterProcess(
            log_file=self.master_log_file,
            enable_console_log=False,
        )
        master._create_pipes()
        master.radio_log_file = self.radio_log_file

        # Re-setup radio logging with low maxBytes to test rotation
        if hasattr(master, "radio_file_handler") and master.radio_file_handler:
            master.radio_logger.removeHandler(master.radio_file_handler)
            master.radio_file_handler.close()

        master.radio_file_handler = RotatingFileHandler(
            str(self.radio_log_file),
            maxBytes=200,  # small size to easily trigger rotation
            backupCount=3,
            encoding="utf-8"
        )
        master.radio_file_handler.setFormatter(
            logging.Formatter("%(asctime)s - [%(name)s] %(message)s")
        )
        master.radio_logger.addHandler(master.radio_file_handler)

        for ch in master.master_channels.values():
            ch.start()

        # Connect peer to master_sim_radio pipe
        sim_conn = self.master.pipes.get("master_sim_radio", master.pipes.get("master_sim"))[1] if hasattr(self, "master") else master.pipes.get("master_sim_radio", master.pipes.get("master_sim"))[1]
        sim_ch = IPCChannel(sim_conn, name="test_peer_sim")
        sim_ch.start()

        # Send several radio log messages
        for i in range(10):
            sim_ch.notify("radio_log", {
                "timestamp": time.time(),
                "source": "airplane_simulator",
                "text": f"Radio transmission message #{i}: test payload with sufficient length to trigger file rotation"
            })

        # Wait for notifications to be processed
        found = False
        for _ in range(40):
            time.sleep(0.05)
            master.radio_file_handler.flush()
            if self.radio_log_file.exists() and self.radio_log_file.stat().st_size > 0:
                found = True
                break

        self.assertTrue(found, "radio.log was not created or written to")

        # Check content in radio.log
        content = self.radio_log_file.read_text(encoding="utf-8")
        self.assertIn("[radio]", content)
        self.assertIn("airplane_simulator", content)

        # Check that rotated backup file exists
        rotated_file = self.log_dir / "radio.log.1"
        self.assertTrue(rotated_file.exists(), f"Rotated log file {rotated_file} was not generated")

        # Cleanup
        sim_ch.close()
        for ch in master.master_channels.values():
            ch.close()
        for p1, p2 in master.pipes.values():
            try:
                p1.close()
            except Exception:
                pass
            try:
                p2.close()
            except Exception:
                pass
        master.radio_file_handler.close()
        master.file_handler.close()
        logging.getLogger().removeHandler(master.file_handler)


class TestWebviewRadioEndpoints(unittest.TestCase):
    """Verifies that Webview buffers radio logs and exposes GET /api/radio_log pagination."""

    def setUp(self):
        self.wv = WebviewApp(
            conn_simulator=None,
            conn_pilot=None,
            conn_controller=None,
            conn_master=None,
        )
        self.app = self.wv.app
        self.client = TestClient(self.app)

    def test_radio_log_rest_api_get_and_post(self):
        # 1. Initially empty
        res = self.client.get("/api/radio_log")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["logs"]), 0)

        # 2. Add logs via POST /api/radio_log
        for i in range(5):
            post_res = self.client.post("/api/radio_log", json={
                "source": "agent_controller",
                "text": f"Instruction {i}",
                "timestamp": 1700000000 + i,
            })
            self.assertEqual(post_res.status_code, 200)

        # 3. Retrieve all
        res = self.client.get("/api/radio_log?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["logs"]), 5)
        self.assertEqual(data["logs"][-1]["text"], "Instruction 4")

        # 4. Test pagination: limit 2, offset 0
        res_p0 = self.client.get("/api/radio_log?limit=2&offset=0")
        self.assertEqual(res_p0.status_code, 200)
        p0_data = res_p0.json()
        self.assertEqual(p0_data["total"], 5)
        self.assertEqual(len(p0_data["logs"]), 2)
        self.assertEqual(p0_data["logs"][0]["text"], "Instruction 0")
        self.assertEqual(p0_data["logs"][1]["text"], "Instruction 1")
        self.assertEqual(p0_data["logs"][0]["timestamp"], 1700000000)

        # 5. Test pagination: limit 2, offset 1
        res_paginated = self.client.get("/api/radio_log?limit=2&offset=1")
        self.assertEqual(res_paginated.status_code, 200)
        p_data = res_paginated.json()
        self.assertEqual(p_data["total"], 5)
        self.assertEqual(len(p_data["logs"]), 2)
        self.assertEqual(p_data["logs"][0]["text"], "Instruction 1")
        self.assertEqual(p_data["logs"][1]["text"], "Instruction 2")

    def test_ipc_radio_log_buffering(self):
        # Simulate IPC message handling
        asyncio.run(self.wv._ipc_handle_radio_log({
            "source": "airplane_simulator",
            "text": "Direct simulated radio communication",
            "timestamp": 1700001000,
        }))

        res = self.client.get("/api/radio_log")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["logs"][0]["text"], "Direct simulated radio communication")
        self.assertEqual(data["logs"][0]["source"], "airplane_simulator")


class TestCLIRadioFeature(unittest.TestCase):
    """Verifies that CLI tool accesses radio history strictly via Webview HTTP endpoint."""

    @patch.object(cli_module, "http_get")
    def test_get_radio_logs_calls_correct_endpoint(self, mock_http_get):
        mock_http_get.return_value = {
            "status": "success",
            "total": 2,
            "logs": [
                {"timestamp": 1700000000, "source": "controller", "text": "Climb 5000"},
                {"timestamp": 1700000010, "source": "pilot", "text": "Roger climbing 5000"}
            ]
        }

        logs_data = cli_module.get_radio_logs("http://test-webview:8000", limit=10, offset=2)
        mock_http_get.assert_called_once_with("http://test-webview:8000/api/radio_log?limit=10&offset=2")
        self.assertEqual(logs_data["total"], 2)
        self.assertEqual(len(logs_data["logs"]), 2)

    def test_print_radio_logs_table(self):
        logs = [
            {"timestamp": 1700000000, "source": "agent_controller", "type": "voice", "text": "Turn heading 270"},
            {"timestamp": 1700000005, "source": "agent_pilot", "type": "control_params", "text": "Roger 270"}
        ]
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli_module.print_radio_logs_table(logs)
        output = buf.getvalue()
        self.assertIn("agent_controller", output)
        self.assertIn("Turn heading 270", output)
        self.assertIn("agent_pilot", output)

    def test_parser_radio_subcommand(self):
        parser = cli_module.build_parser()
        args = parser.parse_args(["radio", "--limit", "25", "--offset", "5", "--json"])
        self.assertEqual(args.command, "radio")
        self.assertEqual(args.limit, 25)
        self.assertEqual(args.offset, 5)
        self.assertTrue(args.output_json)


class TestEndToEndRadioIntegration(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end integration test connecting:
    AgentController -> AirplaneSimulatorService -> AgentPilot -> Simulator physics
    with simultaneous broadcast to MasterProcess (radio.log) and WebviewApp (/api/radio_log).
    """

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)
        self.master_log = self.log_dir / "master.log"
        self.radio_log = self.log_dir / "radio.log"

        # Create master process with rotating radio log
        self.master = MasterProcess(
            log_file=self.master_log,
            radio_log_file=self.radio_log,
            enable_console_log=False,
        )
        self.master._create_pipes()
        for ch in self.master.master_channels.values():
            ch.start()

        # Connect Simulator with separated radio and flight queues
        self.sim_service = AirplaneSimulatorService(
            conn_radio_controller=self.master.pipes["ctrl_sim_radio"][1],
            conn_radio_pilot=self.master.pipes["sim_pilot_radio"][0],
            conn_radio_webview=self.master.pipes["sim_webview_radio"][0],
            conn_radio_master=self.master.pipes["master_sim_radio"][1],
            conn_flight_controller=self.master.pipes["ctrl_sim_flight"][1],
            conn_flight_pilot=self.master.pipes["sim_pilot_flight"][0],
            conn_flight_webview=self.master.pipes["webview_sim_flight"][1],
            conn_flight_master=self.master.pipes["master_sim_flight"][1],
        )

        # Connect Webview
        self.wv_app = WebviewApp(
            conn_radio_simulator=self.master.pipes["sim_webview_radio"][1],
            conn_flight_simulator=self.master.pipes["webview_sim_flight"][0],
            conn_flight_controller=self.master.pipes["webview_ctrl_flight"][0],
            conn_flight_pilot=self.master.pipes["webview_pilot_flight"][0],
            conn_flight_master=self.master.pipes["master_webview_flight"][1],
        )

        # Connect Pilot
        self.pilot = AgentPilot(
            conn_radio_simulator=self.master.pipes["sim_pilot_radio"][1],
            conn_flight_simulator=self.master.pipes["sim_pilot_flight"][1],
            conn_flight_webview=self.master.pipes["webview_pilot_flight"][1],
            conn_flight_master=self.master.pipes["master_pilot_flight"][1],
        )

        # Connect Controller
        self.controller = AgentController(
            conn_radio_simulator=self.master.pipes["ctrl_sim_radio"][0],
            conn_flight_simulator=self.master.pipes["ctrl_sim_flight"][0],
            conn_flight_webview=self.master.pipes["webview_ctrl_flight"][1],
            conn_flight_pilot=self.master.pipes["ctrl_pilot_flight"][0],
            conn_flight_master=self.master.pipes["master_ctrl_flight"][1],
        )

        loop = asyncio.get_running_loop()
        self.sim_service.start()
        self.wv_app.start(loop=loop)
        self.pilot.start(loop=loop)
        self.controller.start(loop=loop)

    async def asyncTearDown(self):
        self.controller.stop()
        self.pilot.stop()
        self.wv_app.stop()
        self.sim_service.stop()
        self.master.stop()
        for p1, p2 in self.master.pipes.values():
            try:
                p1.close()
            except Exception:
                pass
            try:
                p2.close()
            except Exception:
                pass
        self.temp_dir.cleanup()

    async def test_full_chain_controller_sim_pilot_master_webview_cli(self):
        # 1. Add an aircraft to simulator
        plane = self.sim_service.simulator.add_object(
            name="AlphaOne",
            lat=34.0,
            lon=-118.0,
            speed=120.0,
            heading=90.0,
            altitude=3000.0,
        )
        pid = plane["id"]

        # 2. Mock AI response for Pilot
        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "AlphaOne", "speed": 220, "heading": 180, "altitude": 6000}'):
            # Controller sends radio command
            cmd_text = "AlphaOne turn heading 180, climb 6000, speed 220"
            res = await self.controller.send_radio_message(cmd_text)

        # 3. Check controller received positive relay confirmation
        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("relayed"))
        pilot_resp = res.get("pilot_response", {})
        self.assertEqual(pilot_resp.get("status"), "success")
        self.assertEqual(pilot_resp.get("resolved_count"), 1)

        # 4. Check simulator aircraft was updated
        updated_plane = self.sim_service.simulator.get_object(pid)
        self.assertEqual(updated_plane["speed_knots"], 220.0)
        self.assertEqual(updated_plane["target_heading"], 180.0)
        self.assertEqual(updated_plane["target_altitude_ft"], 6000.0)

        # 5. Check master radio.log was written
        await asyncio.sleep(0.1)
        self.master.radio_file_handler.flush()
        self.assertTrue(self.radio_log.exists())
        log_content = self.radio_log.read_text(encoding="utf-8")
        self.assertIn("[RADIO]", log_content)
        self.assertIn("AlphaOne turn heading 180", log_content)

        # 6. Check webview radio_logs buffer
        client = TestClient(self.wv_app.app)
        api_res = client.get("/api/radio_log")
        self.assertEqual(api_res.status_code, 200)
        r_data = api_res.json()
        self.assertGreaterEqual(r_data["total"], 1)
        texts = [l.get("text") for l in r_data["logs"]]
        self.assertTrue(any("AlphaOne turn heading 180" in str(t) for t in texts))

        # 7. Check CLI queries Webview HTTP API successfully
        with patch.object(cli_module, "http_get", side_effect=lambda url: client.get(url.replace("http://localhost:8000", "")).json()):
            cli_res = cli_module.get_radio_logs("http://localhost:8000", limit=10)
            self.assertEqual(cli_res["status"], "success")


class TestDualQueueSeparationAndPurity(unittest.IsolatedAsyncioTestCase):
    """
    专门验证双队列物理隔离与无线电内容纯度：
    1. 无线电通信走专属 Radio 管道，物理运动学更新走专属 Flight 管道，互不串流。
    2. 飞行员提取参数后仅记录本地 log，不通过无线电管道或 Webview IPC 发送。
    3. 管制员巡航提示仅记录系统 log，不发送至无线电管道。
    """

    async def asyncSetUp(self):
        # 1. 无线电队列 Pipes
        self.p_ctrl_sim_r_c, self.p_ctrl_sim_r_s = multiprocessing.Pipe(duplex=True)
        self.p_sim_pilot_r_s, self.p_sim_pilot_r_p = multiprocessing.Pipe(duplex=True)
        self.p_sim_wv_r_s, self.p_sim_wv_r_w = multiprocessing.Pipe(duplex=True)
        self.p_sim_master_r_s, self.p_sim_master_r_m = multiprocessing.Pipe(duplex=True)

        # 2. 飞行数据队列 Pipes
        self.p_ctrl_sim_f_c, self.p_ctrl_sim_f_s = multiprocessing.Pipe(duplex=True)
        self.p_sim_pilot_f_s, self.p_sim_pilot_f_p = multiprocessing.Pipe(duplex=True)
        self.p_wv_sim_f_w, self.p_wv_sim_f_s = multiprocessing.Pipe(duplex=True)
        self.p_master_sim_f_m, self.p_master_sim_f_s = multiprocessing.Pipe(duplex=True)

        # 组装模拟器服务
        self.sim_service = AirplaneSimulatorService(
            conn_radio_controller=self.p_ctrl_sim_r_s,
            conn_radio_pilot=self.p_sim_pilot_r_s,
            conn_radio_webview=self.p_sim_wv_r_s,
            conn_radio_master=self.p_sim_master_r_s,
            conn_flight_controller=self.p_ctrl_sim_f_s,
            conn_flight_pilot=self.p_sim_pilot_f_s,
            conn_flight_webview=self.p_wv_sim_f_s,
            conn_flight_master=self.p_master_sim_f_s,
        )
        self.sim_service.start()

        # 组装飞行员
        self.pilot = AgentPilot(
            conn_radio_simulator=self.p_sim_pilot_r_p,
            conn_flight_simulator=self.p_sim_pilot_f_p,
        )

        # 组装管制员
        self.controller = AgentController(
            conn_radio_simulator=self.p_ctrl_sim_r_c,
            conn_flight_simulator=self.p_ctrl_sim_f_c,
        )

        # 组装 Webview 与 Master peer
        self.ch_wv_radio = IPCChannel(self.p_sim_wv_r_w, name="peer_wv_radio")
        self.ch_master_radio = IPCChannel(self.p_sim_master_r_m, name="peer_master_radio")
        self.ch_wv_radio.start(loop=False)
        self.ch_master_radio.start(loop=False)

        loop = asyncio.get_running_loop()
        self.pilot.start(loop=loop)
        self.controller.start(loop=loop)

    async def asyncTearDown(self):
        self.controller.stop()
        self.pilot.stop()
        self.ch_wv_radio.close()
        self.ch_master_radio.close()
        self.sim_service.stop()

    async def test_radio_and_flight_pipes_strict_isolation(self):
        """测试：无线电指令下发走 radio 队列，飞行参数更新走 flight 队列，完全隔离。"""
        # 1. 在模拟器添加飞机
        plane = self.sim_service.simulator.add_object(
            name="TestSeparation",
            lat=35.0,
            lon=139.0,
            speed=200.0,
            heading=90.0,
            altitude=5000.0,
        )
        pid = plane["id"]

        radio_received_at_master = []
        radio_received_at_wv = []
        self.ch_master_radio.register_handler("radio_log", lambda p: radio_received_at_master.append(p))
        self.ch_wv_radio.register_handler("add_radio_log", lambda p: radio_received_at_wv.append(p))

        # 2. 管制员下发无线电指令
        test_voice = "TestSeparation heading 270, climb 12000"
        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "TestSeparation", "heading": 270, "altitude": 12000}'):
            res = await self.controller.send_radio_message(test_voice)

        await asyncio.sleep(0.1)

        # 验证无线电正常转发并由飞行员执行
        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("relayed"))
        self.assertEqual(len(radio_received_at_master), 1)
        self.assertEqual(radio_received_at_master[0]["text"], test_voice)
        self.assertEqual(len(radio_received_at_wv), 1)
        self.assertEqual(radio_received_at_wv[0]["text"], test_voice)

        # 验证物理仿真姿态已由飞行员经由飞行数据管道更新
        updated = self.sim_service.simulator.get_object(pid)
        self.assertEqual(updated["target_heading"], 270.0)
        self.assertEqual(updated["target_altitude_ft"], 12000.0)

    async def test_pilot_extracted_parameters_do_not_pollute_radio_log(self):
        """测试：飞行员从大模型提取参数后，不向无线电管道或 Webview 发送 add_radio_log。"""
        radio_logs = []
        self.ch_wv_radio.register_handler("add_radio_log", lambda p: radio_logs.append(p))

        # 直接触发飞行员的 handle_radio_command
        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "Drone-Pure", "speed": 220, "heading": 180, "altitude": 2000}'), \
             patch.object(self.pilot, "get_simulator_objects", AsyncMock(return_value=[{"id": "pure-uuid", "name": "Drone-Pure"}])), \
             patch.object(self.pilot, "update_simulator_object", AsyncMock(return_value={"status": "success"})):

            with self.assertLogs("pilot", level="INFO") as cm:
                res = await self.pilot.handle_radio_command("Drone-Pure speed 220, heading 180, altitude 2000")

        await asyncio.sleep(0.05)
        self.assertEqual(res["status"], "success")
        # 验证日志中记录了机载解算参数
        self.assertTrue(any("Target: Drone-Pure, Speed: 220, Heading: 180, Alt: 2000" in log for log in cm.output))
        # 验证没有向无线电通道发送任何包含 control_params 的消息
        self.assertEqual(len(radio_logs), 0)

    async def test_controller_cruise_monitoring_does_not_send_to_radio_queue(self):
        """测试：管制员在飞机巡航无需调整时，不向无线电队列发送消息。"""
        sent_radio = []
        self.controller.send_radio_message = AsyncMock(side_effect=lambda t: sent_radio.append(t))

        async def stop_after_sleep(interval):
            self.controller._stop_event.set()

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", return_value="Drone-Cruise"), \
             patch.object(self.controller, "get_controller_state", AsyncMock(return_value={"enabled": True})), \
             patch.object(self.controller, "get_airport", AsyncMock(return_value={"lat": 35.0, "lon": 139.0, "runway_heading": 160})), \
             patch.object(self.controller, "get_objects", AsyncMock(return_value=[{"id": "c-1", "name": "Drone-Cruise", "status": "airborne"}])), \
             patch("asyncio.sleep", side_effect=stop_after_sleep):

            self.controller._stop_event.clear()
            with self.assertLogs("controller", level="INFO") as cm:
                await self.controller.run_loop()

        # 验证输出包含了监控日志
        self.assertTrue(any("does not need flight state changes" in m for m in cm.output))
        # 验证管制员没有向无线电队列发射消息
        self.assertEqual(len(sent_radio), 0)



if __name__ == "__main__":
    unittest.main()
