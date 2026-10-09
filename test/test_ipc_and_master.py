"""
test/test_ipc_and_master.py

单元测试：验证重构后的 IPCChannel 通信机制、对象化服务及 Master 进程管理逻辑。
测试项包括：
1. IPCChannel 双向请求/响应 (Sync & Async)
2. IPCChannel 单向通知 (Notify)
3. IPCChannel 错误异常跨进程传播
4. IPCChannel 超时机制
5. AirplaneSimulatorService IPC 接口响应
6. WebviewApp 通过 IPC 通道操作 Simulator、Pilot 与 Controller
7. MasterProcess 管道拓扑初始化与子进程管理
"""

import asyncio
import multiprocessing
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add src and src/platform to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))

from agent_common.ipc import IPCChannel
from airplane_simulator import AirplaneSimulator, AirplaneSimulatorService
from webview import WebviewApp
from agent_controller.controller import AgentController
from agent_pilot.pilot import AgentPilot
from master import MasterProcess


class TestIPCChannel(unittest.TestCase):
    def setUp(self):
        self.conn1, self.conn2 = multiprocessing.Pipe(duplex=True)
        self.ch1 = IPCChannel(self.conn1, name="ch1")
        self.ch2 = IPCChannel(self.conn2, name="ch2")
        self.ch1.start()
        self.ch2.start()

    def tearDown(self):
        self.ch1.close()
        self.ch2.close()

    def test_sync_request_response(self):
        self.ch2.register_handler("multiply", lambda p: p["a"] * p["b"])
        result = self.ch1.call_sync("multiply", {"a": 6, "b": 7}, timeout=5.0)
        self.assertEqual(result, 42)

    def test_sync_error_propagation(self):
        def failing_handler(p):
            raise ValueError("Something went wrong inside handler")

        self.ch2.register_handler("fail", failing_handler)
        with self.assertRaises(RuntimeError) as ctx:
            self.ch1.call_sync("fail", {}, timeout=5.0)
        self.assertIn("Something went wrong", str(ctx.exception))

    def test_sync_unknown_action(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.ch1.call_sync("nonexistent_action", {}, timeout=5.0)
        self.assertIn("not supported", str(ctx.exception))

    def test_sync_timeout(self):
        def slow_handler(p):
            time.sleep(1.0)
            return "done"

        self.ch2.register_handler("slow", slow_handler)
        with self.assertRaises(TimeoutError):
            self.ch1.call_sync("slow", {}, timeout=0.1)

    def test_notify(self):
        received = []
        event = threading_event = asyncio.Event() if False else None
        done_flag = []

        def notify_handler(p):
            received.append(p)
            done_flag.append(True)

        self.ch2.register_handler("log_event", notify_handler)
        self.ch1.notify("log_event", {"level": "info", "msg": "test_notify"})

        # Wait briefly for notify delivery
        time.sleep(0.1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["msg"], "test_notify")


class TestAsyncIPCChannel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn1, self.conn2 = multiprocessing.Pipe(duplex=True)
        self.ch1 = IPCChannel(self.conn1, name="async_ch1")
        self.ch2 = IPCChannel(self.conn2, name="async_ch2")
        loop = asyncio.get_running_loop()
        self.ch1.start(loop=loop)
        self.ch2.start(loop=loop)

    async def asyncTearDown(self):
        self.ch1.close()
        self.ch2.close()

    async def test_async_call_success(self):
        async def async_handler(p):
            await asyncio.sleep(0.01)
            return f"Processed {p['val']}"

        self.ch2.register_handler("process", async_handler)
        res = await self.ch1.call("process", {"val": 123}, timeout=5.0)
        self.assertEqual(res, "Processed 123")

    async def test_async_call_exception(self):
        async def err_handler(p):
            raise KeyError("Missing key")

        self.ch2.register_handler("err", err_handler)
        with self.assertRaises(RuntimeError) as ctx:
            await self.ch1.call("err", {}, timeout=5.0)
        self.assertIn("Missing key", str(ctx.exception))


class TestSimulatorServiceIPC(unittest.TestCase):
    def setUp(self):
        self.webview_conn, self.sim_conn_webview = multiprocessing.Pipe(duplex=True)
        self.pilot_conn, self.sim_conn_pilot = multiprocessing.Pipe(duplex=True)
        self.master_conn, self.sim_conn_master = multiprocessing.Pipe(duplex=True)

        self.service = AirplaneSimulatorService(
            conn_webview=self.sim_conn_webview,
            conn_pilot=self.sim_conn_pilot,
            conn_master=self.sim_conn_master,
        )
        self.service.start()

        self.ch_webview = IPCChannel(self.webview_conn, name="test_wv->sim")
        self.ch_pilot = IPCChannel(self.pilot_conn, name="test_pilot->sim")
        self.ch_master = IPCChannel(self.master_conn, name="test_master->sim")
        self.ch_webview.start()
        self.ch_pilot.start()
        self.ch_master.start()

    def tearDown(self):
        self.service.stop()
        self.ch_webview.close()
        self.ch_pilot.close()
        self.ch_master.close()

    def test_simulator_service_crud_over_ipc(self):
        # 1. Add object via webview channel
        new_obj = self.ch_webview.call_sync("add_object", {
            "name": "IPC-Flight-1",
            "lat": 30.0,
            "lon": 120.0,
            "speed": 250.0,
            "heading": 90.0,
            "altitude": 10000.0,
        })
        self.assertIsNotNone(new_obj.get("id"))
        self.assertEqual(new_obj["name"], "IPC-Flight-1")
        obj_id = new_obj["id"]

        # 2. Get objects via pilot channel
        objects = self.ch_pilot.call_sync("get_objects")
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["id"], obj_id)

        # 3. Update object via pilot channel
        upd = self.ch_pilot.call_sync("update_object", {
            "id": obj_id,
            "speed": 320.0,
            "heading": 180.0,
            "altitude": 15000.0,
        })
        self.assertTrue(upd["found"])
        self.assertEqual(upd["object"]["speed_knots"], 320.0)

        # 4. Check simulator state via master channel
        st = self.ch_master.call_sync("status")
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["objects_count"], 1)

        # 5. Remove object via webview channel
        removed = self.ch_webview.call_sync("remove_object", {"id": obj_id})
        self.assertTrue(removed)
        objects_after = self.ch_webview.call_sync("get_objects")
        self.assertEqual(len(objects_after), 0)


class TestWebviewAppIPCIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Setup pipes
        self.wv_sim, self.sim_wv = multiprocessing.Pipe(duplex=True)
        self.wv_ctrl, self.ctrl_wv = multiprocessing.Pipe(duplex=True)
        self.wv_pilot, self.pilot_wv = multiprocessing.Pipe(duplex=True)

        self.sim_service = AirplaneSimulatorService(conn_webview=self.sim_wv)
        self.sim_service.start()

        self.wv_app = WebviewApp(
            conn_simulator=self.wv_sim,
            conn_controller=self.wv_ctrl,
            conn_pilot=self.wv_pilot,
        )

        loop = asyncio.get_running_loop()
        self.wv_app.ch_simulator.start(loop=loop)
        self.wv_app.ch_controller.start(loop=loop)
        self.wv_app.ch_pilot.start(loop=loop)

        # Create remote channels representing Controller and Pilot
        self.remote_ctrl = IPCChannel(self.ctrl_wv, name="remote_ctrl")
        self.remote_pilot = IPCChannel(self.pilot_wv, name="remote_pilot")
        self.remote_ctrl.start(loop=loop)
        self.remote_pilot.start(loop=loop)

    async def asyncTearDown(self):
        self.sim_service.stop()
        self.wv_app.ch_simulator.close()
        self.wv_app.ch_controller.close()
        self.wv_app.ch_pilot.close()
        self.remote_ctrl.close()
        self.remote_pilot.close()

    async def test_webview_proxy_test_ai_over_ipc(self):
        # Register test_ai on remote Controller channel
        self.remote_ctrl.register_handler("test_ai", lambda p: {
            "status": "success",
            "source": "agent_controller",
            "available": True,
            "response": "OK"
        })

        # Register test_ai on remote Pilot channel
        self.remote_pilot.register_handler("test_ai", lambda p: {
            "status": "success",
            "source": "agent_pilot",
            "available": True,
            "response": "OK"
        })

        # Call WebviewApp IPC endpoints
        ctrl_res = await self.wv_app.ch_controller.call("test_ai", {"timeout": 10.0})
        self.assertEqual(ctrl_res["status"], "success")
        self.assertEqual(ctrl_res["source"], "agent_controller")

        pilot_res = await self.wv_app.ch_pilot.call("test_ai", {"timeout": 10.0})
        self.assertEqual(pilot_res["status"], "success")
        self.assertEqual(pilot_res["source"], "agent_pilot")

    async def test_webview_proxy_radio_rx_over_ipc(self):
        # Register radio_RX on remote Pilot channel
        self.remote_pilot.register_handler("radio_RX", lambda p: {
            "status": "success",
            "resolved_count": 1,
            "extracted": {"target": "Drone-X", "speed": 200}
        })

        rx_res = await self.wv_app.ch_pilot.call("radio_RX", {"text": "Drone-X speed 200"})
        self.assertEqual(rx_res["status"], "success")
        self.assertEqual(rx_res["resolved_count"], 1)


class TestMasterProcessArchitecture(unittest.TestCase):
    def test_master_process_pipe_topology(self):
        master = MasterProcess()
        master._create_pipes()

        # Verify all necessary pipes are created
        expected_pipes = [
            "webview_sim", "webview_ctrl", "webview_pilot",
            "ctrl_pilot", "pilot_sim",
            "master_webview", "master_sim", "master_ctrl", "master_pilot"
        ]
        for p in expected_pipes:
            self.assertIn(p, master.pipes)
            self.assertEqual(len(master.pipes[p]), 2)

        # Clean up pipe connections
        for p1, p2 in master.pipes.values():
            p1.close()
            p2.close()


class TestAgentControllerIPCFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wv_conn, self.ctrl_wv = multiprocessing.Pipe(duplex=True)
        self.pilot_conn, self.ctrl_pilot = multiprocessing.Pipe(duplex=True)
        self.master_conn, self.ctrl_master = multiprocessing.Pipe(duplex=True)

        self.ch_wv = IPCChannel(self.wv_conn, name="test_wv")
        self.ch_pilot = IPCChannel(self.pilot_conn, name="test_pilot")
        self.ch_master = IPCChannel(self.master_conn, name="test_master")

        self.ctrl = AgentController(
            conn_webview=self.ctrl_wv,
            conn_pilot=self.ctrl_pilot,
            conn_master=self.ctrl_master,
        )

        loop = asyncio.get_running_loop()
        self.ch_wv.start(loop=loop)
        self.ch_pilot.start(loop=loop)
        self.ch_master.start(loop=loop)
        self.ctrl.ch_webview.start(loop=loop)
        self.ctrl.ch_pilot.start(loop=loop)
        self.ctrl.ch_master.start(loop=loop)

    async def asyncTearDown(self):
        self.ch_wv.close()
        self.ch_pilot.close()
        self.ch_master.close()
        self.ctrl.ch_webview.close()
        self.ctrl.ch_pilot.close()
        self.ctrl.ch_master.close()

    async def test_controller_ipc_query_and_commands(self):
        called = []
        self.ch_wv.register_handler("get_controller_state", lambda p: called.append("c_state") or {"enabled": True})
        self.ch_wv.register_handler("get_airport", lambda p: called.append("airport") or {"lat": 30.0, "lon": 120.0, "runway_heading": 90.0})
        self.ch_wv.register_handler("get_objects", lambda p: called.append("objects") or [{"id": "pl-1", "name": "Flight-1", "lat": 30.0, "lon": 120.0}])
        self.ch_wv.register_handler("add_radio_log", lambda p: called.append(("radio_log", p)) or {"status": "ok"})
        self.ch_pilot.register_handler("radio_RX", lambda p: called.append(("radio_rx", p["text"])) or {"status": "success"})

        # Test individual IPC methods
        c_state = await self.ctrl.get_controller_state()
        self.assertTrue(c_state["enabled"])
        self.assertIn("c_state", called)

        airport = await self.ctrl.get_airport()
        self.assertEqual(airport["lat"], 30.0)
        self.assertIn("airport", called)

        objects = await self.ctrl.get_objects()
        self.assertEqual(len(objects), 1)
        self.assertIn("objects", called)

        await self.ctrl.send_radio_message("Flight-1 climb to 10000")
        await asyncio.sleep(0.05)
        self.assertTrue(any(item == ("radio_rx", "Flight-1 climb to 10000") for item in called))

    async def test_controller_run_loop_iterates_via_ipc(self):
        called = []
        self.ch_wv.register_handler("get_controller_state", lambda p: called.append("c_state") or {"enabled": True})
        self.ch_wv.register_handler("get_airport", lambda p: called.append("airport") or {"lat": 30.0, "lon": 120.0, "runway_heading": 90.0})
        self.ch_wv.register_handler("get_objects", lambda p: called.append("objects") or [{"id": "pl-1", "name": "Flight-1", "lat": 30.0, "lon": 120.0}])
        self.ch_pilot.register_handler("radio_RX", lambda p: called.append(("radio_rx", p["text"])) or {"status": "success"})

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", return_value="Flight-1 climb 5000"), \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            try:
                await self.ctrl.run_loop()
            except asyncio.CancelledError:
                pass

        self.assertIn("c_state", called)
        self.assertIn("airport", called)
        self.assertIn("objects", called)
        self.assertTrue(any(item == ("radio_rx", "Flight-1 climb 5000") for item in called))


class TestAgentPilotIPCFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wv_conn, self.pilot_wv = multiprocessing.Pipe(duplex=True)
        self.sim_conn, self.pilot_sim = multiprocessing.Pipe(duplex=True)
        self.ctrl_conn, self.pilot_ctrl = multiprocessing.Pipe(duplex=True)
        self.master_conn, self.pilot_master = multiprocessing.Pipe(duplex=True)

        self.ch_wv = IPCChannel(self.wv_conn, name="test_wv")
        self.ch_sim = IPCChannel(self.sim_conn, name="test_sim")
        self.ch_ctrl = IPCChannel(self.ctrl_conn, name="test_ctrl")

        self.pilot = AgentPilot(
            conn_webview=self.pilot_wv,
            conn_simulator=self.pilot_sim,
            conn_controller=self.pilot_ctrl,
            conn_master=self.pilot_master,
        )

        loop = asyncio.get_running_loop()
        self.ch_wv.start(loop=loop)
        self.ch_sim.start(loop=loop)
        self.ch_ctrl.start(loop=loop)
        self.pilot.start(loop=loop)

    async def asyncTearDown(self):
        self.ch_wv.close()
        self.ch_sim.close()
        self.ch_ctrl.close()
        self.pilot.stop()

    async def test_pilot_command_execution_over_ipc(self):
        called = []
        self.ch_sim.register_handler("get_objects", lambda p: called.append("sim_get_objects") or [{"id": "uuid-99", "name": "Drone-99"}])
        self.ch_sim.register_handler("update_object", lambda p: called.append(("sim_update", p)) or {"found": True, "object": p})
        self.ch_wv.register_handler("add_radio_log", lambda p: called.append(("wv_log", p)) or {"status": "ok"})

        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "Drone-99", "speed": 350, "altitude": 8000}'):
            # Call radio_RX via Controller channel
            res = await self.ch_ctrl.call("radio_RX", {"text": "Drone-99 speed 350, altitude 8000"})
            await asyncio.sleep(0.05)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["resolved_count"], 1)
        self.assertIn("sim_get_objects", called)
        self.assertTrue(any(item[0] == "sim_update" and item[1]["id"] == "uuid-99" for item in called if isinstance(item, tuple)))
        self.assertTrue(any(item[0] == "wv_log" for item in called if isinstance(item, tuple)))


class TestIPCChannelAdvancedFeatures(unittest.TestCase):
    def test_async_handler_in_sync_channel(self):
        conn1, conn2 = multiprocessing.Pipe(duplex=True)
        ch1 = IPCChannel(conn1, name="sync_ch1")
        ch2 = IPCChannel(conn2, name="sync_ch2")
        ch1.start()
        ch2.start()

        # Register async handler on ch2 which runs without event loop
        async def async_handler(p):
            await asyncio.sleep(0.01)
            return p["val"] * 10

        ch2.register_handler("calc", async_handler)
        res = ch1.call_sync("calc", {"val": 5}, timeout=2.0)
        self.assertEqual(res, 50)

        ch1.close()
        ch2.close()

    def test_late_loop_binding(self):
        conn1, conn2 = multiprocessing.Pipe(duplex=True)
        ch1 = IPCChannel(conn1, name="late_ch1")
        ch2 = IPCChannel(conn2, name="late_ch2")
        ch1.start()  # No loop bound initially
        self.assertIsNone(ch1._loop)

        loop = asyncio.new_event_loop()
        ch1.start(loop=loop)
        self.assertEqual(ch1._loop, loop)

        ch1.close()
        ch2.close()
        loop.close()


if __name__ == "__main__":
    unittest.main()
