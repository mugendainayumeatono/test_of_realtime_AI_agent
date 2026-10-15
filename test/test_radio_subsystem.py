"""
test/test_radio_subsystem.py

面向对象无线电广播通信子系统综合单元测试与端到端集成测试：
1. RadioBase 抽象基类与广播信道机制（多频道隔离、发送端不回收、生命周期管理、异步收发、发送前自动记录日志）。
2. IPCRadio 派生类测试（沿用现有无线电 IPC 管道实现频道广播与日志转发）。
3. Controller -> Simulator (Hub) -> Pilot 广播中继与日志分发。
4. Master 集中式独立无线电轮转日志 (logs/radio.log) 写入与轮转。
5. Webview 独立无线电日志缓存、WebSocket 广播与 HTTP GET /api/radio_log 分页查询。
6. CLI 工具对 Webview HTTP 无线电接口的查询与展示。
7. 端到端链路验证：Controller 指令下发 -> IPCRadio 频道广播 -> Pilot 解析解算 -> Simulator 姿态更新，
   同时广播至 Master radio.log 与 Webview /api/radio_log。
8. 双队列（无线电 Radio 专线 vs 飞行数据 Flight 专线）物理隔离与纯度验证。
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
from agent_common.radio import (
    RadioBase,
    IPCRadio,
    InMemoryRadio,
    DEFAULT_RADIO_CHANNEL,
)
from airplane_simulator import AirplaneSimulatorService
from webview import WebviewApp
from agent_controller.controller import AgentController
from agent_pilot.pilot import AgentPilot
from master import MasterProcess
import cli as cli_module


class TestRadioBaseArchitecture(unittest.IsolatedAsyncioTestCase):
    """验证 RadioBase 抽象基类规范与多频道广播机制"""

    def test_radio_base_cannot_be_instantiated_directly(self):
        """纯虚函数约束：未重写 _execute_send 与 _execute_receive 的 RadioBase 无法实例化"""
        with self.assertRaises(TypeError):
            RadioBase(endpoint_id="test")

    async def test_multi_channel_creation_registration_and_isolation(self):
        """
        验证多频道创建、注册加入、多频道互相隔离：
        在频道 A 发送的内容，加入频道 A 的端点可以收到，但仅加入频道 B 的端点绝对收不到。
        """
        bus_id = f"test_bus_{time.time()}"
        radio_sender = InMemoryRadio(endpoint_id="sender", bus_id=bus_id)
        radio_sub_a = InMemoryRadio(endpoint_id="sub_a", bus_id=bus_id)
        radio_sub_b = InMemoryRadio(endpoint_id="sub_b", bus_id=bus_id)

        # 创建频道
        self.assertTrue(radio_sender.create_channel("tower_118_1"))
        self.assertTrue(radio_sender.create_channel("ground_121_9"))

        # 端点注册加入频道
        rec_a = []
        rec_b = []
        radio_sub_a.register("tower_118_1", handler=lambda p: rec_a.append(p))
        radio_sub_b.register("ground_121_9", handler=lambda p: rec_b.append(p))

        self.assertTrue(radio_sub_a.is_joined("tower_118_1"))
        self.assertFalse(radio_sub_a.is_joined("ground_121_9"))
        self.assertTrue(radio_sub_b.is_joined("ground_121_9"))
        self.assertFalse(radio_sub_b.is_joined("tower_118_1"))

        # 在 tower 频道发送消息
        await radio_sender.send("tower_118_1", "Cleared to land runway 24R")
        await asyncio.sleep(0.05)

        # sub_a 应当收到，sub_b 绝不应当收到
        self.assertEqual(len(rec_a), 1)
        self.assertEqual(rec_a[0]["text"], "Cleared to land runway 24R")
        self.assertEqual(len(rec_b), 0)

        # 在 ground 频道发送消息
        await radio_sender.send("ground_121_9", "Taxi via Alpha hold short 24R")
        await asyncio.sleep(0.05)

        # sub_b 应当收到，sub_a 不受影响（仍为 1）
        self.assertEqual(len(rec_a), 1)
        self.assertEqual(len(rec_b), 1)
        self.assertEqual(rec_b[0]["text"], "Taxi via Alpha hold short 24R")

        radio_sender.close()
        radio_sub_a.close()
        radio_sub_b.close()

    async def test_sender_does_not_receive_its_own_transmission(self):
        """广播机制：发送端点不必接收自己发送出去的内容"""
        bus_id = f"test_bus_self_{time.time()}"
        radio = InMemoryRadio(endpoint_id="node_1", bus_id=bus_id)
        peer = InMemoryRadio(endpoint_id="node_2", bus_id=bus_id)

        radio_received = []
        peer_received = []
        radio.register("channel_1", handler=lambda p: radio_received.append(p))
        peer.register("channel_1", handler=lambda p: peer_received.append(p))

        await radio.send("channel_1", "Hello channel 1")
        await asyncio.sleep(0.05)

        # 发送端本身不应收到
        self.assertEqual(len(radio_received), 0)
        # 监听对端应当收到
        self.assertEqual(len(peer_received), 1)
        self.assertEqual(peer_received[0]["text"], "Hello channel 1")

        radio.close()
        peer.close()

    async def test_leave_and_destroy_channel(self):
        """退出频道 (leave_channel) 与销毁频道 (destroy_channel) 接口"""
        bus_id = f"test_bus_ld_{time.time()}"
        sender = InMemoryRadio(endpoint_id="sender", bus_id=bus_id)
        receiver = InMemoryRadio(endpoint_id="receiver", bus_id=bus_id)

        rec = []
        receiver.register("temp_ch", handler=lambda p: rec.append(p))
        self.assertTrue(receiver.is_joined("temp_ch"))

        await sender.send("temp_ch", "Message 1")
        await asyncio.sleep(0.05)
        self.assertEqual(len(rec), 1)

        # 退出频道
        receiver.leave_channel("temp_ch")
        self.assertFalse(receiver.is_joined("temp_ch"))

        await sender.send("temp_ch", "Message 2")
        await asyncio.sleep(0.05)
        # 退出后不再接收新消息
        self.assertEqual(len(rec), 1)

        # 销毁频道
        sender.destroy_channel("temp_ch")
        self.assertFalse(sender.is_channel_active("temp_ch"))

        sender.close()
        receiver.close()

    async def test_async_receive_queue_interface(self):
        """异步 receive() 接口测试"""
        bus_id = f"test_bus_recv_{time.time()}"
        sender = InMemoryRadio(endpoint_id="sender", bus_id=bus_id)
        receiver = InMemoryRadio(endpoint_id="receiver", bus_id=bus_id)

        receiver.register("queue_ch")
        await sender.send("queue_ch", {"text": "Async queued payload"})

        # 通过异步 receive 读取
        data = await receiver.receive("queue_ch", timeout=1.0)
        self.assertEqual(data["text"], "Async queued payload")
        self.assertEqual(data["source"], "sender")

        sender.close()
        receiver.close()

    async def test_log_recorded_before_send(self):
        """规范要求：调用底层发送前必须自动记录无线电 log"""
        log_records = []

        class MockRadio(RadioBase):
            def __init__(self, endpoint_id):
                super().__init__(endpoint_id=endpoint_id, log_sender=lambda p: log_records.append(p))
                self.send_called = False

            async def _execute_send(self, channel_name: str, payload: dict, **kwargs):
                # 验证调用 _execute_send 时，log 已先被记录
                self.send_called = True
                self.log_count_at_send = len(log_records)
                return {"status": "success"}

            async def _execute_receive(self, channel_name: str, timeout=None, **kwargs):
                pass

        mock_r = MockRadio("log_tester")
        mock_r.register("test_ch")
        await mock_r.send("test_ch", "Check log precedence")

        self.assertTrue(mock_r.send_called)
        self.assertEqual(mock_r.log_count_at_send, 1)
        self.assertEqual(len(log_records), 1)
        self.assertEqual(log_records[0]["text"], "Check log precedence")
        self.assertEqual(log_records[0]["channel"], "test_ch")


class TestIPCRadioMultiChannel(unittest.IsolatedAsyncioTestCase):
    """验证基于 IPC 管道的 IPCRadio 异步通信与多频道隔离"""

    async def asyncSetUp(self):
        self.p1, self.p2 = multiprocessing.Pipe(duplex=True)
        self.r1 = IPCRadio(endpoint_id="peer_1", connection=self.p1)
        self.r2 = IPCRadio(endpoint_id="peer_2", connection=self.p2)

        loop = asyncio.get_running_loop()
        self.r1.start(loop=loop)
        self.r2.start(loop=loop)

    async def asyncTearDown(self):
        self.r1.close()
        self.r2.close()

    async def test_ipc_point_to_point_channel_communication(self):
        """两个端点通过 IPCRadio 管道进行频道收发"""
        r2_received = []
        self.r2.register("ch_atc", handler=lambda p: r2_received.append(p))

        await self.r1.send("ch_atc", "Flight 101 climb FL300")
        await asyncio.sleep(0.05)

        self.assertEqual(len(r2_received), 1)
        self.assertEqual(r2_received[0]["text"], "Flight 101 climb FL300")
        self.assertEqual(r2_received[0]["source"], "peer_1")

    async def test_ipc_channel_isolation(self):
        """验证 IPC 管道环境下不同频道的物理隔离"""
        r2_received_ch2 = []
        self.r2.register("ch_different", handler=lambda p: r2_received_ch2.append(p))

        # r1 向 ch_one 发送，r2 只订阅了 ch_different
        await self.r1.send("ch_one", "Message for channel one only")
        await asyncio.sleep(0.05)

        self.assertEqual(len(r2_received_ch2), 0)


class TestRadioRelaySubsystem(unittest.IsolatedAsyncioTestCase):
    """验证 Controller 发送无线电指令经由 Simulator 广播中继转发至 Pilot 及日志端点"""

    async def asyncSetUp(self):
        # 1. Controller <-> Simulator
        self.p_ctrl_sim_c, self.p_ctrl_sim_s = multiprocessing.Pipe(duplex=True)
        # 2. Simulator <-> Pilot
        self.p_sim_pilot_s, self.p_sim_pilot_p = multiprocessing.Pipe(duplex=True)
        # 3. Simulator <-> Master
        self.p_sim_master_s, self.p_sim_master_m = multiprocessing.Pipe(duplex=True)
        # 4. Simulator <-> Webview
        self.p_sim_wv_s, self.p_sim_wv_w = multiprocessing.Pipe(duplex=True)

        # Simulator service (IPCRadio Hub)
        self.sim_service = AirplaneSimulatorService(
            conn_radio_webview=self.p_sim_wv_s,
            conn_radio_pilot=self.p_sim_pilot_s,
            conn_radio_master=self.p_sim_master_s,
            conn_radio_controller=self.p_ctrl_sim_s,
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
            conn_radio_simulator=self.p_ctrl_sim_c,
        )
        loop = asyncio.get_running_loop()
        with patch("agent_controller.controller.get_ai_model", return_value=False), \
             patch("agent_controller.controller.test_ai_api"):
            self.controller.start(loop=loop)

    async def asyncTearDown(self):
        self.controller.stop()
        self.ch_pilot.close()
        self.ch_master.close()
        self.ch_webview.close()
        self.sim_service.stop()

    async def test_controller_relays_radio_via_simulator_to_pilot_and_logs(self):
        """Controller.send_radio_message -> Simulator.radio (Hub) -> Pilot 广播, Master.radio_log, Webview.add_radio_log."""
        pilot_received = []
        master_logs = []
        webview_logs = []

        # Pilot 监听广播消息
        def handle_pilot_broadcast(params):
            pilot_received.append(params)

        def handle_master_log(params):
            master_logs.append(params)

        def handle_webview_log(params):
            webview_logs.append(params)

        self.ch_pilot.register_handler("radio_broadcast", handle_pilot_broadcast)
        self.ch_pilot.register_handler("radio_RX", handle_pilot_broadcast)
        self.ch_master.register_handler("radio_log", handle_master_log)
        self.ch_webview.register_handler("add_radio_log", handle_webview_log)

        # 模拟 Pilot 端点加入 default 频道
        self.ch_pilot.notify("radio_register", {"endpoint": "agent_pilot", "channel": "default"})
        await asyncio.sleep(0.05)

        # Controller 发送无线电指令
        test_msg = "Drone-42 climb to 9000, speed 220"
        res = await self.controller.send_radio_message(test_msg)

        # 异步广播传播等待
        await asyncio.sleep(0.1)

        # 1. 验证 Controller 方法返回
        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("relayed"))

        # 2. 验证 Pilot 端点收到广播报文
        self.assertEqual(len(pilot_received), 1)
        self.assertEqual(pilot_received[0]["text"], test_msg)
        self.assertEqual(pilot_received[0]["source"], "agent_controller")

        # 3. 验证 Master 收到独立无线电日志
        self.assertEqual(len(master_logs), 1)
        self.assertEqual(master_logs[0]["text"], test_msg)
        self.assertEqual(master_logs[0]["source"], "agent_controller")

        # 4. 验证 Webview 收到独立无线电日志
        self.assertEqual(len(webview_logs), 1)
        self.assertEqual(webview_logs[0]["text"], test_msg)
        self.assertEqual(webview_logs[0]["source"], "agent_controller")


class TestMasterCentralizedRadioLogging(unittest.TestCase):
    """验证 Master 进程创建并维护专用的独立 logs/radio.log 文件及其轮转"""

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

        if hasattr(master, "radio_file_handler") and master.radio_file_handler:
            master.radio_logger.removeHandler(master.radio_file_handler)
            master.radio_file_handler.close()

        master.radio_file_handler = RotatingFileHandler(
            str(self.radio_log_file),
            maxBytes=200,
            backupCount=3,
            encoding="utf-8"
        )
        master.radio_file_handler.setFormatter(
            logging.Formatter("%(asctime)s - [%(name)s] %(message)s")
        )
        master.radio_logger.addHandler(master.radio_file_handler)

        for ch in master.master_channels.values():
            ch.start()

        sim_conn = master.pipes["master_sim_radio"][1]
        sim_ch = IPCChannel(sim_conn, name="test_peer_sim")
        sim_ch.start()

        for i in range(10):
            sim_ch.notify("radio_log", {
                "timestamp": time.time(),
                "source": "airplane_simulator",
                "text": f"Radio transmission message #{i}: test payload with sufficient length to trigger file rotation"
            })

        found = False
        for _ in range(40):
            time.sleep(0.05)
            master.radio_file_handler.flush()
            if self.radio_log_file.exists() and self.radio_log_file.stat().st_size > 0:
                found = True
                break

        self.assertTrue(found, "radio.log was not created or written to")

        content = self.radio_log_file.read_text(encoding="utf-8")
        self.assertIn("[radio]", content)
        self.assertIn("airplane_simulator", content)

        rotated_file = self.log_dir / "radio.log.1"
        self.assertTrue(rotated_file.exists(), f"Rotated log file {rotated_file} was not generated")

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
    """验证 Webview 缓存无线电日志并暴露 GET /api/radio_log 分页查询接口"""

    def setUp(self):
        self.wv = WebviewApp()
        self.app = self.wv.app
        self.client = TestClient(self.app)

    def test_radio_log_rest_api_get_and_post(self):
        # 1. 初始为空
        res = self.client.get("/api/radio_log")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["logs"]), 0)

        # 2. 通过 POST /api/radio_log 添加日志
        for i in range(5):
            post_res = self.client.post("/api/radio_log", json={
                "source": "agent_controller",
                "text": f"Instruction {i}",
                "timestamp": 1700000000 + i,
            })
            self.assertEqual(post_res.status_code, 200)

        # 3. 获取全量
        res = self.client.get("/api/radio_log?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["logs"]), 5)
        self.assertEqual(data["logs"][-1]["text"], "Instruction 4")

        # 4. 分页：limit 2, offset 0
        res_p0 = self.client.get("/api/radio_log?limit=2&offset=0")
        self.assertEqual(res_p0.status_code, 200)
        p0_data = res_p0.json()
        self.assertEqual(p0_data["total"], 5)
        self.assertEqual(len(p0_data["logs"]), 2)
        self.assertEqual(p0_data["logs"][0]["text"], "Instruction 0")
        self.assertEqual(p0_data["logs"][1]["text"], "Instruction 1")

        # 5. 分页：limit 2, offset 1
        res_paginated = self.client.get("/api/radio_log?limit=2&offset=1")
        self.assertEqual(res_paginated.status_code, 200)
        p_data = res_paginated.json()
        self.assertEqual(p_data["total"], 5)
        self.assertEqual(len(p_data["logs"]), 2)
        self.assertEqual(p_data["logs"][0]["text"], "Instruction 1")
        self.assertEqual(p_data["logs"][1]["text"], "Instruction 2")

    def test_ipc_radio_log_buffering(self):
        # 模拟通过管道处理无线电日志
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
    """验证 CLI 工具严格通过 Webview HTTP 接口查询并打印无线电日志历史"""

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
    全系统端到端集成链路测试：
    AgentController -> IPCRadio (广播) -> AirplaneSimulatorService (Hub) -> AgentPilot -> 模拟器物理姿态更新，
    同时日志广播至 MasterProcess (logs/radio.log) 与 WebviewApp (/api/radio_log)。
    """

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)
        self.master_log = self.log_dir / "master.log"
        self.radio_log = self.log_dir / "radio.log"

        self.master = MasterProcess(
            log_file=self.master_log,
            radio_log_file=self.radio_log,
            enable_console_log=False,
        )
        self.master._create_pipes()
        for ch in self.master.master_channels.values():
            ch.start()

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

        self.wv_app = WebviewApp(
            conn_radio_simulator=self.master.pipes["sim_webview_radio"][1],
            conn_flight_simulator=self.master.pipes["webview_sim_flight"][0],
            conn_flight_controller=self.master.pipes["webview_ctrl_flight"][0],
            conn_flight_pilot=self.master.pipes["webview_pilot_flight"][0],
            conn_flight_master=self.master.pipes["master_webview_flight"][1],
        )

        self.pilot = AgentPilot(
            conn_radio_simulator=self.master.pipes["sim_pilot_radio"][1],
            conn_flight_simulator=self.master.pipes["sim_pilot_flight"][1],
            conn_flight_webview=self.master.pipes["webview_pilot_flight"][1],
            conn_flight_master=self.master.pipes["master_pilot_flight"][1],
        )

        self.controller = AgentController(
            conn_radio_simulator=self.master.pipes["ctrl_sim_radio"][0],
            conn_flight_simulator=self.master.pipes["ctrl_sim_flight"][0],
            conn_flight_webview=self.master.pipes["webview_ctrl_flight"][1],
            conn_flight_master=self.master.pipes["master_ctrl_flight"][1],
        )

        loop = asyncio.get_running_loop()
        self.sim_service.start()
        self.wv_app.start(loop=loop)
        self.pilot.start(loop=loop)
        with patch("agent_controller.controller.get_ai_model", return_value=False), \
             patch("agent_controller.controller.test_ai_api"):
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
        # 1. 模拟器添加飞机
        plane = self.sim_service.simulator.add_object(
            name="AlphaOne",
            lat=34.0,
            lon=-118.0,
            speed=120.0,
            heading=90.0,
            altitude=3000.0,
        )
        pid = plane["id"]

        # 2. Mock 飞行员 AI 解析响应
        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "AlphaOne", "speed": 220, "heading": 180, "altitude": 6000}'):
            cmd_text = "AlphaOne turn heading 180, climb 6000, speed 220"
            res = await self.controller.send_radio_message(cmd_text)

            # 3. 验证管制员无线电广播发送成功
            self.assertEqual(res.get("status"), "success")
            self.assertTrue(res.get("relayed"))

            # 等待飞行员经由广播接收后，调用模拟器飞行数据管道更新飞机
            for _ in range(30):
                await asyncio.sleep(0.05)
                updated_plane = self.sim_service.simulator.get_object(pid)
                if updated_plane.get("target_heading") == 180.0:
                    break

        # 4. 验证飞机物理状态已被更新
        updated_plane = self.sim_service.simulator.get_object(pid)
        self.assertEqual(updated_plane["speed_knots"], 220.0)
        self.assertEqual(updated_plane["target_heading"], 180.0)
        self.assertEqual(updated_plane["target_altitude_ft"], 6000.0)

        # 5. 验证 Master 独立 radio.log 写入
        self.master.radio_file_handler.flush()
        self.assertTrue(self.radio_log.exists())
        log_content = self.radio_log.read_text(encoding="utf-8")
        self.assertIn("[RADIO]", log_content)
        self.assertIn("AlphaOne turn heading 180", log_content)

        # 6. 验证 Webview 缓存
        client = TestClient(self.wv_app.app)
        api_res = client.get("/api/radio_log")
        self.assertEqual(api_res.status_code, 200)
        r_data = api_res.json()
        self.assertGreaterEqual(r_data["total"], 1)
        texts = [l.get("text") for l in r_data["logs"]]
        self.assertTrue(any("AlphaOne turn heading 180" in str(t) for t in texts))

        # 7. 验证 CLI 通过 Webview 接口成功查询
        with patch.object(cli_module, "http_get", side_effect=lambda url: client.get(url.replace("http://localhost:8000", "")).json()):
            cli_res = cli_module.get_radio_logs("http://localhost:8000", limit=10)
            self.assertEqual(cli_res["status"], "success")


class TestDualQueueSeparationAndPurity(unittest.IsolatedAsyncioTestCase):
    """验证无线电专线与飞行控制专线完全物理隔离及纯净度"""

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

        self.pilot = AgentPilot(
            conn_radio_simulator=self.p_sim_pilot_r_p,
            conn_flight_simulator=self.p_sim_pilot_f_p,
        )

        self.controller = AgentController(
            conn_radio_simulator=self.p_ctrl_sim_r_c,
            conn_flight_simulator=self.p_ctrl_sim_f_c,
        )

        self.ch_wv_radio = IPCChannel(self.p_sim_wv_r_w, name="peer_wv_radio")
        self.ch_master_radio = IPCChannel(self.p_sim_master_r_m, name="peer_master_radio")
        self.ch_wv_radio.start(loop=False)
        self.ch_master_radio.start(loop=False)

        loop = asyncio.get_running_loop()
        self.pilot.start(loop=loop)
        with patch("agent_controller.controller.get_ai_model", return_value=False), \
             patch("agent_controller.controller.test_ai_api"):
            self.controller.start(loop=loop)

    async def asyncTearDown(self):
        self.controller.stop()
        self.pilot.stop()
        self.ch_wv_radio.close()
        self.ch_master_radio.close()
        self.sim_service.stop()

    async def test_radio_and_flight_pipes_strict_isolation(self):
        """测试：无线电指令下发走 radio 队列，飞行参数更新走 flight 队列，完全隔离。"""
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

        test_voice = "TestSeparation heading 270, climb 12000"
        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "TestSeparation", "heading": 270, "altitude": 12000}'):
            res = await self.controller.send_radio_message(test_voice)

            # 等待广播处理与姿态更新
            for _ in range(30):
                await asyncio.sleep(0.05)
                updated = self.sim_service.simulator.get_object(pid)
                if updated.get("target_heading") == 270.0:
                    break

        self.assertEqual(res.get("status"), "success")
        self.assertTrue(res.get("relayed"))
        self.assertEqual(len(radio_received_at_master), 1)
        self.assertEqual(radio_received_at_master[0]["text"], test_voice)
        self.assertEqual(len(radio_received_at_wv), 1)
        self.assertEqual(radio_received_at_wv[0]["text"], test_voice)

        # 验证物理姿态更新
        updated = self.sim_service.simulator.get_object(pid)
        self.assertEqual(updated["target_heading"], 270.0)
        self.assertEqual(updated["target_altitude_ft"], 12000.0)

    async def test_pilot_extracted_parameters_do_not_pollute_radio_log(self):
        """测试：飞行员从大模型提取参数后，不向无线电管道或 Webview 发送 add_radio_log。"""
        radio_logs = []
        self.ch_wv_radio.register_handler("add_radio_log", lambda p: radio_logs.append(p))

        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "Drone-Pure", "speed": 220, "heading": 180, "altitude": 2000}'), \
             patch.object(self.pilot, "get_simulator_objects", AsyncMock(return_value=[{"id": "pure-uuid", "name": "Drone-Pure"}])), \
             patch.object(self.pilot, "update_simulator_object", AsyncMock(return_value={"status": "success"})):

            with self.assertLogs("pilot", level="INFO") as cm:
                res = await self.pilot.handle_radio_command("Drone-Pure speed 220, heading 180, altitude 2000")

        await asyncio.sleep(0.05)
        self.assertEqual(res["status"], "success")
        self.assertTrue(any("Target: Drone-Pure, Speed: 220, Heading: 180, Alt: 2000" in log for log in cm.output))
        self.assertEqual(len(radio_logs), 0)

    async def test_controller_cruise_monitoring_does_not_send_to_radio_queue(self):
        """测试：管制员在飞机巡航无需调整时，不向无线电队列发送消息。"""
        sent_radio = []
        self.controller.send_radio_message = AsyncMock(side_effect=lambda t: sent_radio.append(t))

        async def stop_after_sleep(interval):
            self.controller._stop_event.set()

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", return_value=json.dumps([{"id": "Drone-Cruise", "is_schedule": False, "msg": ""}])), \
             patch.object(self.controller, "get_controller_state", AsyncMock(return_value={"enabled": True})), \
             patch.object(self.controller, "get_airport", AsyncMock(return_value={"lat": 35.0, "lon": 139.0, "runway_heading": 160})), \
             patch.object(self.controller, "get_objects", AsyncMock(return_value=[{"id": "c-1", "name": "Drone-Cruise", "status": "airborne"}])), \
             patch("asyncio.sleep", side_effect=stop_after_sleep):

            self.controller._stop_event.clear()
            with self.assertLogs("controller", level="INFO") as cm:
                await self.controller.run_loop()

        self.assertTrue(any("does not need flight state changes" in m for m in cm.output))
        self.assertEqual(len(sent_radio), 0)


class TestRadioAdvancedFeaturesAndEdgeCases(unittest.IsolatedAsyncioTestCase):
    """
    加强测试：针对无线电通信子系统的边界条件、容错逻辑及高级特性的专项测试
    """

    async def test_sender_with_custom_callsign_source_does_not_receive_own_message(self):
        """当发送方指定自定义呼号为 source（与自身的 endpoint_id 不同）时，基于 origin 判定绝不接收自身广播"""
        bus_id = f"test_bus_callsign_{time.time()}"
        r1 = InMemoryRadio(endpoint_id="controller_endpoint", bus_id=bus_id)
        r2 = InMemoryRadio(endpoint_id="pilot_endpoint", bus_id=bus_id)

        r1_received = []
        r2_received = []
        r1.register("tower", handler=lambda p: r1_received.append(p))
        r2.register("tower", handler=lambda p: r2_received.append(p))

        # Controller 以 "Tokyo Tower" 作为业务呼号发送指令
        await r1.send("tower", "JA-NRT-4000 descend 3000", source="Tokyo Tower")
        await asyncio.sleep(0.05)

        # r1（控制器物理端点）绝不应该收到自己发出的内容
        self.assertEqual(len(r1_received), 0)
        # r2（飞行员物理端点）应当正常收到
        self.assertEqual(len(r2_received), 1)
        self.assertEqual(r2_received[0]["text"], "JA-NRT-4000 descend 3000")
        self.assertEqual(r2_received[0]["source"], "Tokyo Tower")
        self.assertEqual(r2_received[0]["origin"], "controller_endpoint")

        r1.close()
        r2.close()

    async def test_unjoined_channel_receive_raises_error(self):
        """未注册加入频道直接调用 receive() 应抛出 RuntimeError"""
        radio = InMemoryRadio(endpoint_id="tester", bus_id=f"test_unjoined_{time.time()}")
        with self.assertRaises(RuntimeError) as cm:
            await radio.receive("unjoined_ch", timeout=0.1)
        self.assertIn("has not registered to channel 'unjoined_ch'", str(cm.exception))
        radio.close()

    async def test_ipc_radio_async_receive_queue_and_isolation(self):
        """验证 IPCRadio 端点可以通过异步 await receive() 接口无回调接收数据"""
        p1, p2 = multiprocessing.Pipe(duplex=True)
        r1 = IPCRadio(endpoint_id="ipc_sender", connection=p1)
        r2 = IPCRadio(endpoint_id="ipc_receiver", connection=p2)

        loop = asyncio.get_running_loop()
        r1.start(loop=loop)
        r2.start(loop=loop)

        r2.register("radar_channel")
        await r1.send("radar_channel", {"text": "Squawk 7700", "code": 7700})

        msg = await r2.receive("radar_channel", timeout=1.0)
        self.assertEqual(msg["text"], "Squawk 7700")
        self.assertEqual(msg["channel"], "radar_channel")

        r1.close()
        r2.close()

    async def test_partial_handler_leave_channel(self):
        """同一频道注册多个 handler 时，注销单个 handler 不影响其他 handler 接收"""
        bus_id = f"test_bus_partial_{time.time()}"
        radio = InMemoryRadio(endpoint_id="endpoint", bus_id=bus_id)
        sender = InMemoryRadio(endpoint_id="sender", bus_id=bus_id)

        h1_called = []
        h2_called = []

        def h1(p):
            h1_called.append(p)

        def h2(p):
            h2_called.append(p)

        radio.register("multi_handler_ch", handler=h1)
        radio.register("multi_handler_ch", handler=h2)

        await sender.send("multi_handler_ch", "First broadcast")
        await asyncio.sleep(0.05)
        self.assertEqual(len(h1_called), 1)
        self.assertEqual(len(h2_called), 1)

        # 注销 h1，h2 应当继续生效
        radio.leave_channel("multi_handler_ch", handler=h1)
        self.assertTrue(radio.is_joined("multi_handler_ch"))

        await sender.send("multi_handler_ch", "Second broadcast")
        await asyncio.sleep(0.05)
        self.assertEqual(len(h1_called), 1)
        self.assertEqual(len(h2_called), 2)

        # 注销 h2，端点完全退出频道
        radio.leave_channel("multi_handler_ch", handler=h2)
        self.assertFalse(radio.is_joined("multi_handler_ch"))

        await sender.send("multi_handler_ch", "Third broadcast")
        await asyncio.sleep(0.05)
        self.assertEqual(len(h2_called), 2)

        radio.close()
        sender.close()

    async def test_radio_api_aliases(self):
        """验证 join_channel, unregister 等频道管理别名函数"""
        bus_id = f"test_bus_aliases_{time.time()}"
        r1 = InMemoryRadio(endpoint_id="alias_1", bus_id=bus_id)
        r2 = InMemoryRadio(endpoint_id="alias_2", bus_id=bus_id)

        received = []
        # join_channel 别名
        self.assertTrue(r2.join_channel("alias_ch", handler=lambda p: received.append(p)))
        self.assertTrue(r2.is_joined("alias_ch"))

        # send 发送消息
        await r1.send("alias_ch", "Transmit message")
        await asyncio.sleep(0.05)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["text"], "Transmit message")

        # log 无线电记录
        log_payload = r1.log(text="Test log radio", channel_name="alias_ch")
        self.assertEqual(log_payload["text"], "Test log radio")

        # unregister 别名
        self.assertTrue(r2.unregister("alias_ch"))
        self.assertFalse(r2.is_joined("alias_ch"))

        r1.close()
        r2.close()

    async def test_coroutine_and_awaitable_handlers(self):
        """验证支持异步协程函数及返回 awaitable 的回调处理程序"""
        bus_id = f"test_bus_async_{time.time()}"
        sender = InMemoryRadio(endpoint_id="async_sender", bus_id=bus_id)
        receiver = InMemoryRadio(endpoint_id="async_receiver", bus_id=bus_id)

        async_results = []

        async def async_handler(payload):
            await asyncio.sleep(0.01)
            async_results.append(payload["text"])

        receiver.register("coro_ch", handler=async_handler)
        await sender.send("coro_ch", "Coroutine test payload")
        await asyncio.sleep(0.05)

        self.assertEqual(len(async_results), 1)
        self.assertEqual(async_results[0], "Coroutine test payload")

        sender.close()
        receiver.close()

    async def test_non_dict_data_send_and_log(self):
        """测试发送非 dict 非 str 类型（数字/原始对象）的无线电报文"""
        bus_id = f"test_bus_nondict_{time.time()}"
        sender = InMemoryRadio(endpoint_id="nondict_sender", bus_id=bus_id)
        receiver = InMemoryRadio(endpoint_id="nondict_receiver", bus_id=bus_id)

        recv = []
        receiver.register("numeric_ch", handler=lambda p: recv.append(p))

        # 发送整数 12345
        await sender.send("numeric_ch", 12345)
        await asyncio.sleep(0.05)

        self.assertEqual(len(recv), 1)
        self.assertEqual(recv[0]["text"], "12345")
        self.assertEqual(recv[0]["data"], 12345)

        sender.close()
        receiver.close()

    async def test_hub_broadcasts_its_own_transmissions(self):
        """验证 Simulator 自身作为 Hub 发起无线电广播时，能正确向加入频道的对端转发并分发日志"""
        p_hub_ctrl, p_ctrl_hub = multiprocessing.Pipe(duplex=True)
        p_hub_pilot, p_pilot_hub = multiprocessing.Pipe(duplex=True)
        p_hub_master, p_master_hub = multiprocessing.Pipe(duplex=True)
        p_hub_wv, p_wv_hub = multiprocessing.Pipe(duplex=True)

        hub_radio = IPCRadio(
            endpoint_id="airplane_simulator",
            peer_connections={
                "agent_controller": p_hub_ctrl,
                "agent_pilot": p_hub_pilot,
                "master": p_hub_master,
                "webview": p_hub_wv,
            },
            is_hub=True,
        )

        pilot_ch = IPCChannel(p_pilot_hub, name="pilot_peer")
        master_ch = IPCChannel(p_master_hub, name="master_peer")
        wv_ch = IPCChannel(p_wv_hub, name="wv_peer")

        loop = asyncio.get_running_loop()
        pilot_ch.start(loop=loop)
        master_ch.start(loop=loop)
        wv_ch.start(loop=loop)
        hub_radio.start(loop=loop)

        pilot_received = []
        master_logs = []
        wv_logs = []

        pilot_ch.register_handler("radio_broadcast", lambda p: pilot_received.append(p))
        master_ch.register_handler("radio_log", lambda p: master_logs.append(p))
        wv_ch.register_handler("add_radio_log", lambda p: wv_logs.append(p))

        # 飞行员注册加入 atis_ch
        pilot_ch.notify("radio_register", {"endpoint": "agent_pilot", "channel": "atis_ch"})
        await asyncio.sleep(0.05)

        # 模拟器 Hub 自身播发 ATIS 信息
        res = await hub_radio.send("atis_ch", "Tokyo ATIS Information Delta", source="RJTT_ATIS")
        await asyncio.sleep(0.05)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(len(pilot_received), 1)
        self.assertEqual(pilot_received[0]["text"], "Tokyo ATIS Information Delta")
        self.assertEqual(pilot_received[0]["source"], "RJTT_ATIS")
        self.assertEqual(len(master_logs), 1)
        self.assertEqual(len(wv_logs), 1)

        hub_radio.close()
        pilot_ch.close()
        master_ch.close()
        wv_ch.close()

    async def test_ipc_channel_lifecycle_create_leave_destroy_via_hub(self):
        """测试通过 IPCRadio 管道跨进程进行频道创建、退出与销毁生命周期的联动控制"""
        p_hub_c, p_c_hub = multiprocessing.Pipe(duplex=True)
        p_hub_p, p_p_hub = multiprocessing.Pipe(duplex=True)

        hub_radio = IPCRadio(
            endpoint_id="airplane_simulator",
            peer_connections={"agent_controller": p_hub_c, "agent_pilot": p_hub_p},
            is_hub=True,
        )
        ctrl_radio = IPCRadio(endpoint_id="agent_controller", connection=p_c_hub)
        pilot_radio = IPCRadio(endpoint_id="agent_pilot", connection=p_p_hub)

        loop = asyncio.get_running_loop()
        hub_radio.start(loop=loop)
        ctrl_radio.start(loop=loop)
        pilot_radio.start(loop=loop)

        pilot_messages = []
        pilot_radio.register("dynamic_channel", handler=lambda p: pilot_messages.append(p))
        await asyncio.sleep(0.05)

        # 1. 验证加入后可以接收
        await ctrl_radio.send("dynamic_channel", "Test Msg 1")
        await asyncio.sleep(0.05)
        self.assertEqual(len(pilot_messages), 1)

        # 2. 退出频道后不能接收
        pilot_radio.leave_channel("dynamic_channel")
        await asyncio.sleep(0.05)
        await ctrl_radio.send("dynamic_channel", "Test Msg 2")
        await asyncio.sleep(0.05)
        self.assertEqual(len(pilot_messages), 1)

        # 3. 销毁频道
        ctrl_radio.destroy_channel("dynamic_channel")
        await asyncio.sleep(0.05)
        self.assertFalse(ctrl_radio.is_channel_active("dynamic_channel"))

        hub_radio.close()
        ctrl_radio.close()
        pilot_radio.close()

    def test_standalone_radio_log_without_ipc(self):
        """当无 IPC 管道连接时，IPCRadio.log() 安全记录本地 payload 返回，不发起无谓 HTTP 请求"""
        standalone_radio = IPCRadio(endpoint_id="standalone")
        payload = standalone_radio.log(text="Standalone test message")
        self.assertEqual(payload["text"], "Standalone test message")
        self.assertEqual(payload["source"], "standalone")


if __name__ == "__main__":
    unittest.main()
