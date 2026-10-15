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
import io
import logging
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add src and src/platform to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))

from agent_common.ipc import (
    IPCChannel,
    IPCLogHandler,
    record_to_dict,
    dict_to_record,
    setup_child_logging,
)
from airplane_simulator import AirplaneSimulator, AirplaneSimulatorService, simulator_process_entrypoint
from webview import WebviewApp, webview_process_entrypoint
from agent_controller.controller import AgentController, controller_process_entrypoint
from agent_pilot.pilot import AgentPilot, pilot_process_entrypoint
from master import MasterProcess, setup_master_logging, get_master_pid, stop_master, status_master, PID_FILE
import main as master_main




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
            conn_flight_webview=self.sim_conn_webview,
            conn_flight_pilot=self.sim_conn_pilot,
            conn_flight_master=self.sim_conn_master,
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

        self.sim_service = AirplaneSimulatorService(conn_flight_webview=self.sim_wv)
        self.sim_service.start()

        self.wv_app = WebviewApp(
            conn_flight_simulator=self.wv_sim,
            conn_flight_controller=self.wv_ctrl,
            conn_flight_pilot=self.wv_pilot,
        )

        loop = asyncio.get_running_loop()
        self.wv_app.ch_flight_sim.start(loop=loop)
        self.wv_app.ch_flight_ctrl.start(loop=loop)
        self.wv_app.ch_flight_pilot.start(loop=loop)

        # Create remote channels representing Controller and Pilot
        self.remote_ctrl = IPCChannel(self.ctrl_wv, name="remote_ctrl")
        self.remote_pilot = IPCChannel(self.pilot_wv, name="remote_pilot")
        self.remote_ctrl.start(loop=loop)
        self.remote_pilot.start(loop=loop)

    async def asyncTearDown(self):
        self.sim_service.stop()
        self.wv_app.ch_flight_sim.close()
        self.wv_app.ch_flight_ctrl.close()
        self.wv_app.ch_flight_pilot.close()
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
        ctrl_res = await self.wv_app.ch_flight_ctrl.call("test_ai", {"timeout": 10.0})
        self.assertEqual(ctrl_res["status"], "success")
        self.assertEqual(ctrl_res["source"], "agent_controller")

        pilot_res = await self.wv_app.ch_flight_pilot.call("test_ai", {"timeout": 10.0})
        self.assertEqual(pilot_res["status"], "success")
        self.assertEqual(pilot_res["source"], "agent_pilot")

    async def test_webview_proxy_radio_rx_over_ipc(self):
        # Register radio_RX on remote Pilot channel
        self.remote_pilot.register_handler("radio_RX", lambda p: {
            "status": "success",
            "resolved_count": 1,
            "extracted": {"target": "Drone-X", "speed": 200}
        })

        rx_res = await self.wv_app.ch_flight_pilot.call("radio_RX", {"text": "Drone-X speed 200"})
        self.assertEqual(rx_res["status"], "success")
        self.assertEqual(rx_res["resolved_count"], 1)

    async def test_webview_controller_state_ipc_integration(self):
        ctrl_state = {"enabled": False}
        self.remote_ctrl.register_handler("get_controller_state", lambda p: dict(ctrl_state))
        def handle_set(p):
            ctrl_state["enabled"] = bool(p.get("enabled", False))
            return {"status": "success", "enabled": ctrl_state["enabled"]}
        self.remote_ctrl.register_handler("set_controller_state", handle_set)

        # 1. Webview queries controller state over IPC
        st1 = await self.wv_app.get_controller_state_via_ipc()
        self.assertFalse(st1["enabled"])

        # 2. Webview sets controller state over IPC
        set_res = await self.wv_app.set_controller_state_via_ipc(True)
        self.assertEqual(set_res["status"], "success")
        self.assertTrue(set_res["enabled"])
        self.assertTrue(ctrl_state["enabled"])

        # 3. Webview verifies updated state over IPC
        st2 = await self.wv_app.get_controller_state_via_ipc()
        self.assertTrue(st2["enabled"])


class TestMasterProcessArchitecture(unittest.TestCase):
    def test_master_process_pipe_topology(self):
        master = MasterProcess()
        master._create_pipes()

        expected_pipes = [
            "ctrl_sim_radio", "sim_pilot_radio", "sim_webview_radio", "master_sim_radio",
            "ctrl_sim_flight", "sim_pilot_flight", "webview_sim_flight",
            "webview_ctrl_flight", "webview_pilot_flight",
            "master_sim_flight", "master_webview_flight", "master_ctrl_flight", "master_pilot_flight"
        ]
        for p in expected_pipes:
            self.assertIn(p, master.pipes)
            self.assertEqual(len(master.pipes[p]), 2)

        # Clean up pipe connections
        for p1, p2 in master.pipes.values():
            p1.close()
            p2.close()

    def test_child_process_entrypoints_kwargs_compatibility(self):
        """测试 Master 传入各子进程的参数字典均能正确绑定对应 entrypoint 签名，防止 TypeError 崩溃"""
        import inspect

        ctrl_kwargs = {
            "conn_radio_simulator": None,
            "conn_flight_simulator": None,
            "conn_flight_webview": None,
            "conn_flight_master": None,
        }
        inspect.signature(controller_process_entrypoint).bind(**ctrl_kwargs)

        pilot_kwargs = {
            "conn_radio_simulator": None,
            "conn_flight_simulator": None,
            "conn_flight_webview": None,
            "conn_flight_master": None,
        }
        inspect.signature(pilot_process_entrypoint).bind(**pilot_kwargs)

        sim_kwargs = {
            "conn_radio_controller": None,
            "conn_radio_pilot": None,
            "conn_radio_webview": None,
            "conn_radio_master": None,
            "conn_flight_controller": None,
            "conn_flight_pilot": None,
            "conn_flight_webview": None,
            "conn_flight_master": None,
        }
        inspect.signature(simulator_process_entrypoint).bind(**sim_kwargs)

        wv_kwargs = {
            "conn_radio_simulator": None,
            "conn_flight_simulator": None,
            "conn_flight_controller": None,
            "conn_flight_pilot": None,
            "conn_flight_master": None,
            "host": "0.0.0.0",
            "port": 8000,
        }
        inspect.signature(webview_process_entrypoint).bind(**wv_kwargs)

    def test_master_process_start_and_teardown_all_children(self):
        """端到端测试：Master 完整拉起 4 个核心业务子进程，验证均正常运行未崩溃，并优雅退出"""
        master = MasterProcess(host="127.0.0.1", port=8098, auto_restart=False)
        master._create_pipes()
        master._start_child_processes()

        try:
            self.assertEqual(len(master.processes), 4)
            # 等待片刻确保子进程完成初始化
            time.sleep(1.0)
            for name, proc in master.processes.items():
                self.assertTrue(proc.is_alive(), f"Subprocess [{name}] (PID: {proc.pid}) died with code {proc.exitcode}")
                self.assertIsNone(proc.exitcode, f"Subprocess [{name}] unexpectedly exited with code {proc.exitcode}")
        finally:
            master.stop()

        for name, proc in master.processes.items():
            self.assertFalse(proc.is_alive(), f"Subprocess [{name}] should have exited")

    def test_master_process_auto_restart_child_on_failure(self):
        """测试：子进程异常退出（如被 kill）时，Master 自动重建管理管道并重新拉起新实例"""
        master = MasterProcess(host="127.0.0.1", port=8096, auto_restart=False)
        master._create_pipes()
        master._start_child_processes()

        try:
            time.sleep(1.0)
            ctrl_proc = master.processes["agent_controller"]
            old_pid = ctrl_proc.pid
            self.assertTrue(ctrl_proc.is_alive())

            # 模拟子进程意外崩溃
            ctrl_proc.kill()
            ctrl_proc.join(timeout=2.0)
            self.assertFalse(ctrl_proc.is_alive())

            # 触发重启逻辑
            master._restart_process("agent_controller")
            time.sleep(1.0)

            new_proc = master.processes["agent_controller"]
            self.assertNotEqual(new_proc.pid, old_pid)
            self.assertTrue(new_proc.is_alive())
            self.assertIsNone(new_proc.exitcode)
        finally:
            master.stop()

    def test_master_cli_and_pid_lifecycle(self):
        """测试 Master PID 文件记录、状态查询与停止函数"""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_pid_file = Path(temp_dir) / "master.pid"
            with patch("master.PID_FILE", test_pid_file):
                # 1. PID 文件不存在时
                self.assertIsNone(get_master_pid())
                # status 应提示未运行
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    status_master()
                    self.assertIn("STOPPED", mock_out.getvalue())

                # 2. 写入不存在的死 PID
                test_pid_file.write_text("99999999", encoding="utf-8")
                self.assertIsNone(get_master_pid())

                # 3. 写入当前进程真实 PID
                test_pid_file.write_text(str(os.getpid()), encoding="utf-8")
                self.assertEqual(get_master_pid(), os.getpid())

                # 4. stop_master 当 pid 不存在时安全返回 False
                test_pid_file.unlink()
                self.assertFalse(stop_master())

    def test_main_launcher_argument_dispatch(self):
        """测试 main.py 的 CLI 参数解析与分发分流"""
        with patch("main.status_master") as mock_status, \
             patch("main.stop_master") as mock_stop:

            master_main.main(["status"])
            mock_status.assert_called_once()

            master_main.main(["stop"])
            mock_stop.assert_called_once()




class TestAgentControllerIPCFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wv_conn, self.ctrl_wv = multiprocessing.Pipe(duplex=True)
        self.sim_conn_flight, self.ctrl_sim_flight = multiprocessing.Pipe(duplex=True)
        self.sim_conn_radio, self.ctrl_sim_radio = multiprocessing.Pipe(duplex=True)
        self.master_conn, self.ctrl_master = multiprocessing.Pipe(duplex=True)

        self.ch_wv = IPCChannel(self.wv_conn, name="test_wv")
        self.ch_sim_flight = IPCChannel(self.sim_conn_flight, name="test_sim_flight")
        self.ch_sim_radio = IPCChannel(self.sim_conn_radio, name="test_sim_radio")
        self.ch_master = IPCChannel(self.master_conn, name="test_master")

        self.ctrl = AgentController(
            conn_flight_webview=self.ctrl_wv,
            conn_flight_simulator=self.ctrl_sim_flight,
            conn_radio_simulator=self.ctrl_sim_radio,
            conn_flight_master=self.ctrl_master,
        )

        loop = asyncio.get_running_loop()
        self.ch_wv.start(loop=loop)
        self.ch_sim_flight.start(loop=loop)
        self.ch_sim_radio.start(loop=loop)
        self.ch_master.start(loop=loop)
        if self.ctrl.radio:
            self.ctrl.radio.start(loop=loop)
            self.ctrl.radio.create_channel("default")
            self.ctrl.radio.register("default")
        if self.ctrl.ch_flight_sim:
            self.ctrl.ch_flight_sim.start(loop=loop)
        if self.ctrl.ch_flight_webview:
            self.ctrl.ch_flight_webview.start(loop=loop)
        if self.ctrl.ch_flight_master:
            self.ctrl.ch_flight_master.start(loop=loop)

    async def asyncTearDown(self):
        self.ch_wv.close()
        self.ch_sim_flight.close()
        self.ch_sim_radio.close()
        self.ch_master.close()
        self.ctrl.stop()

    async def test_controller_ipc_query_and_commands(self):
        called = []
        self.ch_sim_flight.register_handler("get_airport", lambda p: called.append("airport") or {"lat": 30.0, "lon": 120.0, "runway_heading": 90.0})
        self.ch_sim_flight.register_handler("get_objects", lambda p: called.append("objects") or [{"id": "pl-1", "name": "Flight-1", "lat": 30.0, "lon": 120.0}])
        self.ch_sim_radio.register_handler("radio_broadcast", lambda p: called.append(("radio_broadcast", p.get("text", ""))) or {"status": "success"})
        self.ch_sim_radio.register_handler("radio_log", lambda p: {"status": "success"})
        self.ch_sim_radio.register_handler("radio_create", lambda p: {"status": "success"})
        self.ch_sim_radio.register_handler("radio_register", lambda p: {"status": "success"})

        # Controller starts initialized to disabled state
        self.assertFalse(self.ctrl.enabled)
        initial_state = await self.ch_wv.call("get_controller_state")
        self.assertFalse(initial_state["enabled"])

        # Enable controller via IPC from Webview
        set_res = await self.ch_wv.call("set_controller_state", {"enabled": True})
        self.assertEqual(set_res["status"], "success")
        self.assertTrue(set_res["enabled"])
        self.assertTrue(self.ctrl.enabled)

        # Test individual IPC methods
        c_state = await self.ctrl.get_controller_state()
        self.assertTrue(c_state["enabled"])

        airport = await self.ctrl.get_airport()
        self.assertEqual(airport["lat"], 30.0)
        self.assertIn("airport", called)

        objects = await self.ctrl.get_objects()
        self.assertEqual(len(objects), 1)
        self.assertIn("objects", called)

        await self.ctrl.send_radio_message("Flight-1 climb to 10000")
        await asyncio.sleep(0.05)
        self.assertTrue(any(item == ("radio_broadcast", "Flight-1 climb to 10000") for item in called))

    async def test_controller_run_loop_iterates_via_ipc(self):
        called = []
        self.ch_sim_flight.register_handler("get_airport", lambda p: called.append("airport") or {"lat": 30.0, "lon": 120.0, "runway_heading": 90.0})
        self.ch_sim_flight.register_handler("get_objects", lambda p: called.append("objects") or [{"id": "pl-1", "name": "Flight-1", "lat": 30.0, "lon": 120.0}])
        self.ch_sim_radio.register_handler("radio_broadcast", lambda p: called.append(("radio_broadcast", p.get("text", ""))) or {"status": "success"})
        self.ch_sim_radio.register_handler("radio_log", lambda p: {"status": "success"})
        self.ch_sim_radio.register_handler("radio_create", lambda p: {"status": "success"})
        self.ch_sim_radio.register_handler("radio_register", lambda p: {"status": "success"})

        # Enable controller so run loop iterates
        self.ctrl.set_controller_state(True)

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", return_value="Flight-1 climb 5000"), \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            try:
                await self.ctrl.run_loop()
            except asyncio.CancelledError:
                pass

        await asyncio.sleep(0.05)
        self.assertIn("airport", called)
        self.assertIn("objects", called)
        self.assertTrue(any(item == ("radio_broadcast", "Flight-1 climb 5000") for item in called))


class TestAgentPilotIPCFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wv_conn, self.pilot_wv = multiprocessing.Pipe(duplex=True)
        self.sim_conn, self.pilot_sim = multiprocessing.Pipe(duplex=True)
        self.radio_conn, self.pilot_radio = multiprocessing.Pipe(duplex=True)
        self.master_conn, self.pilot_master = multiprocessing.Pipe(duplex=True)

        self.ch_wv = IPCChannel(self.wv_conn, name="test_wv")
        self.ch_sim = IPCChannel(self.sim_conn, name="test_sim")
        self.ch_radio = IPCChannel(self.radio_conn, name="test_radio")

        self.pilot = AgentPilot(
            conn_flight_webview=self.pilot_wv,
            conn_flight_simulator=self.pilot_sim,
            conn_radio_simulator=self.pilot_radio,
            conn_flight_master=self.pilot_master,
        )

        loop = asyncio.get_running_loop()
        self.ch_wv.start(loop=loop)
        self.ch_sim.start(loop=loop)
        self.ch_radio.start(loop=loop)
        self.pilot.start(loop=loop)

    async def asyncTearDown(self):
        self.ch_wv.close()
        self.ch_sim.close()
        self.ch_radio.close()
        self.pilot.stop()

    async def test_pilot_command_execution_over_ipc(self):
        called = []
        self.ch_sim.register_handler("get_objects", lambda p: called.append("sim_get_objects") or [{"id": "uuid-99", "name": "Drone-99"}])
        self.ch_sim.register_handler("update_object", lambda p: called.append(("sim_update", p)) or {"found": True, "object": p})
        self.ch_wv.register_handler("add_radio_log", lambda p: called.append(("wv_log", p)) or {"status": "ok"})

        with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
             patch("agent_pilot.pilot.generate_ai_response", return_value='{"target": "Drone-99", "speed": 350, "altitude": 8000}'):
            # Call radio_RX via simulator radio channel
            res = await self.ch_radio.call("radio_RX", {"text": "Drone-99 speed 350, altitude 8000"})
            await asyncio.sleep(0.05)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["resolved_count"], 1)
        self.assertIn("sim_get_objects", called)
        self.assertTrue(any(item[0] == "sim_update" and item[1]["id"] == "uuid-99" for item in called if isinstance(item, tuple)))
        self.assertFalse(any(item[0] == "wv_log" for item in called if isinstance(item, tuple)))


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


class TestCentralizedIPCLogging(unittest.TestCase):
    def test_record_to_dict_and_dict_to_record_roundtrip(self):
        logger = logging.getLogger("test.logger")
        try:
            raise ValueError("Test error for traceback")
        except ValueError:
            exc_info = sys.exc_info()

        record = logger.makeRecord(
            name="test.logger",
            level=logging.ERROR,
            fn="test_file.py",
            lno=42,
            msg="Formatted message with %s and %d",
            args=("arg1", 100),
            exc_info=exc_info,
            func="test_func",
            extra={"custom_tag": "sensor_event", "device_id": 999}
        )
        record.processName = "test_process"

        d = record_to_dict(record)
        self.assertEqual(d["name"], "test.logger")
        self.assertEqual(d["levelname"], "ERROR")
        self.assertEqual(d["levelno"], logging.ERROR)
        self.assertEqual(d["msg"], "Formatted message with arg1 and 100")
        self.assertEqual(d["processName"], "test_process")
        self.assertEqual(d["custom_tag"], "sensor_event")
        self.assertEqual(d["device_id"], 999)
        self.assertIn("ValueError: Test error for traceback", d["exc_text"])

        restored = dict_to_record(d)
        self.assertEqual(restored.name, "test.logger")
        self.assertEqual(restored.levelname, "ERROR")
        self.assertEqual(restored.levelno, logging.ERROR)
        self.assertEqual(restored.getMessage(), "Formatted message with arg1 and 100")
        self.assertEqual(restored.processName, "test_process")
        self.assertEqual(getattr(restored, "custom_tag", None), "sensor_event")
        self.assertEqual(getattr(restored, "device_id", None), 999)
        self.assertIn("ValueError: Test error for traceback", restored.exc_text)

        # Test defaults on incomplete payload
        sparse = dict_to_record({})
        self.assertEqual(sparse.levelno, logging.INFO)
        self.assertEqual(sparse.levelname, "INFO")
        self.assertEqual(sparse.name, "unknown")
        self.assertEqual(sparse.msg, "")
        self.assertTrue(bool(sparse.processName))
        self.assertIsNotNone(sparse.created)

        with_proc = dict_to_record({"processName": "child_worker"})
        self.assertEqual(with_proc.processName, "child_worker")

        # Test non-dict raises TypeError
        with self.assertRaises(TypeError):
            dict_to_record("invalid")

    def test_ipc_log_handler_forwarding(self):
        conn1, conn2 = multiprocessing.Pipe(duplex=True)
        ch_client = IPCChannel(conn1, name="child_log")
        ch_master = IPCChannel(conn2, name="master_log")
        ch_master.start()

        received_logs = []
        event = threading.Event()

        def handle_log(p):
            received_logs.append(p)
            event.set()

        ch_master.register_handler("log", handle_log)

        handler = IPCLogHandler(channel=ch_client, process_name="test_worker")
        test_logger = logging.getLogger("test_worker_logger")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        test_logger.propagate = False

        test_logger.info("Hello from child via IPC!")
        self.assertTrue(event.wait(timeout=2.0))

        self.assertEqual(len(received_logs), 1)
        self.assertEqual(received_logs[0]["name"], "test_worker_logger")
        self.assertEqual(received_logs[0]["msg"], "Hello from child via IPC!")
        self.assertEqual(received_logs[0]["processName"], "test_worker")

        test_logger.removeHandler(handler)
        handler.close()
        ch_client.close()
        ch_master.close()

    def test_ipc_log_handler_fallback_when_channel_none(self):
        stream = io.StringIO()
        fallback_handler = logging.StreamHandler(stream)
        fallback_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

        handler = IPCLogHandler(channel=None, fallback_handler=fallback_handler)
        test_logger = logging.getLogger("test_fallback_logger")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        test_logger.propagate = False

        test_logger.warning("Channel is None fallback")
        output = stream.getvalue()
        self.assertIn("WARNING: Channel is None fallback", output)

        test_logger.removeHandler(handler)
        handler.close()

    def test_ipc_log_handler_fallback_when_channel_closed_or_broken(self):
        conn1, conn2 = multiprocessing.Pipe(duplex=True)
        ch_client = IPCChannel(conn1, name="broken_client")
        ch_master = IPCChannel(conn2, name="broken_master")
        ch_master.close()

        stream = io.StringIO()
        fallback_handler = logging.StreamHandler(stream)
        fallback_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

        handler = IPCLogHandler(channel=ch_client, fallback_handler=fallback_handler)
        test_logger = logging.getLogger("test_broken_logger")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        test_logger.propagate = False

        test_logger.error("Channel broken fallback")
        output = stream.getvalue()
        self.assertIn("ERROR: Channel broken fallback", output)

        test_logger.removeHandler(handler)
        handler.close()
        ch_client.close()

    def test_anti_recursion_in_ipc_log_handler(self):
        # Create a mock channel whose notify causes a log call
        mock_ch = MagicMock()
        mock_ch._stopped.is_set.return_value = False
        handler = IPCLogHandler(channel=mock_ch)
        test_logger = logging.getLogger("test_recursion_logger")
        test_logger.addHandler(handler)
        test_logger.propagate = False

        call_count = [0]
        def recursive_notify(action, params):
            call_count[0] += 1
            if call_count[0] < 5:
                test_logger.info("Recursive log inside notify")
            return True

        mock_ch.notify.side_effect = recursive_notify
        # Should not get into infinite recursion
        test_logger.info("Trigger log")
        self.assertEqual(call_count[0], 1)

        test_logger.removeHandler(handler)
        handler.close()

    def test_handle_child_log_edge_cases_corrupt_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "master.log"
            master = MasterProcess(log_file=log_file, enable_console_log=False)
            try:
                # Empty dict - should produce safe record without error
                master._handle_child_log({})
                # Missing keys
                master._handle_child_log({"msg": "Only msg", "levelno": 20})
                # Invalid type should be safely caught without exception
                master._handle_child_log(None)
                master._handle_child_log("non-dict")

                master.file_handler.flush()
                content = log_file.read_text(encoding="utf-8")
                self.assertIn("Only msg", content)
            finally:
                master.file_handler.close()
                logging.getLogger().removeHandler(master.file_handler)

    def test_child_services_logging_initialization(self):
        # Test AgentController without and with master conn
        ctrl1 = AgentController()
        self.assertIsNotNone(ctrl1.ipc_log_handler)
        self.assertIsNone(ctrl1.ipc_log_handler.channel)

        p1, p2 = multiprocessing.Pipe()
        ctrl2 = AgentController(conn_flight_master=p1)
        self.assertIsNotNone(ctrl2.ipc_log_handler)
        self.assertIsNotNone(ctrl2.ipc_log_handler.channel)
        ctrl2.stop()
        p2.close()

        # Test AgentPilot without and with master conn
        pilot1 = AgentPilot()
        self.assertIsNotNone(pilot1.ipc_log_handler)
        self.assertIsNone(pilot1.ipc_log_handler.channel)

        p3, p4 = multiprocessing.Pipe()
        pilot2 = AgentPilot(conn_flight_master=p3)
        self.assertIsNotNone(pilot2.ipc_log_handler)
        self.assertIsNotNone(pilot2.ipc_log_handler.channel)
        pilot2.stop()
        p4.close()

        # Test AirplaneSimulatorService without and with master conn
        sim1 = AirplaneSimulatorService()
        self.assertIsNotNone(sim1.ipc_log_handler)
        self.assertIsNone(sim1.ipc_log_handler.channel)

        p5, p6 = multiprocessing.Pipe()
        sim2 = AirplaneSimulatorService(conn_flight_master=p5)
        self.assertIsNotNone(sim2.ipc_log_handler)
        self.assertIsNotNone(sim2.ipc_log_handler.channel)
        sim2.stop()
        p6.close()

        # Test WebviewApp without and with master conn
        wv1 = WebviewApp()
        self.assertIsNotNone(wv1.ipc_log_handler)
        self.assertIsNone(wv1.ipc_log_handler.channel)

        p7, p8 = multiprocessing.Pipe()
        wv2 = WebviewApp(conn_flight_master=p7)
        self.assertIsNotNone(wv2.ipc_log_handler)
        self.assertIsNotNone(wv2.ipc_log_handler.channel)
        wv2.stop()
        p8.close()


class TestMasterCentralizedLoggingAndRotation(unittest.TestCase):
    def test_master_logging_aggregation_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "master.log"
            max_bytes = 1000
            backup_count = 3

            master = MasterProcess(
                log_file=log_file,
                max_log_bytes=max_bytes,
                log_backup_count=backup_count,
                enable_console_log=False,
            )

            components = [
                ("controller", "Controller dispatched flight plan A", "agent_controller"),
                ("pilot", "Pilot adjusted speed to 250kts", "agent_pilot"),
                ("platform.airplane_simulator", "Physics simulation step computed", "airplane_simulator"),
                ("platform.webview", "WebSocket client connected 127.0.0.1", "webview"),
            ]

            for name, msg, proc in components:
                record = logging.makeLogRecord({
                    "name": name,
                    "levelno": logging.INFO,
                    "levelname": "INFO",
                    "msg": msg,
                    "created": time.time(),
                    "processName": proc,
                })
                master._handle_child_log(record_to_dict(record))

            master.file_handler.flush()
            self.assertTrue(log_file.exists())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("Controller dispatched flight plan A", content)
            self.assertIn("Pilot adjusted speed to 250kts", content)
            # Verify processName is rendered in centralized master log
            self.assertIn("[agent_controller]", content)
            self.assertIn("[agent_pilot]", content)
            self.assertIn("[airplane_simulator]", content)
            self.assertIn("[webview]", content)

            # Write enough logs to exceed max_bytes multiple times and trigger rotation
            for i in range(80):
                record = logging.makeLogRecord({
                    "name": "stress_logger",
                    "levelno": logging.INFO,
                    "levelname": "INFO",
                    "msg": f"Stress log line #{i:03d} to trigger log rotation with sufficient byte length",
                    "created": time.time(),
                })
                master._handle_child_log(record_to_dict(record))

            master.file_handler.flush()
            rotated_files = list(Path(tmpdir).glob("master.log*"))
            # Should have master.log and rotated files (e.g., master.log.1, master.log.2)
            self.assertGreaterEqual(len(rotated_files), 2)
            self.assertLessEqual(len(rotated_files), backup_count + 1)

            master.file_handler.close()
            logging.getLogger().removeHandler(master.file_handler)

    def test_uvicorn_log_propagation_through_ipc_handler(self):
        import uvicorn
        conn1, conn2 = multiprocessing.Pipe(duplex=True)
        ch_client = IPCChannel(conn1, name="uvicorn_client")
        ch_master = IPCChannel(conn2, name="uvicorn_master")
        ch_master.start()

        received_logs = []
        event = threading.Event()

        def handle_log(p):
            received_logs.append(p)
            if len(received_logs) >= 2:
                event.set()

        ch_master.register_handler("log", handle_log)

        ipc_handler = setup_child_logging(channel=ch_client, process_name="webview")

        # Simulate uvicorn startup with log_config=None
        _ = uvicorn.Config(app=None, log_config=None, log_level="info")
        logging.getLogger("uvicorn").info("Uvicorn running on http://0.0.0.0:8000")
        logging.getLogger("uvicorn.access").info("GET /api/airport HTTP/1.1 200 OK")

        self.assertTrue(event.wait(timeout=2.0))
        messages = [l["msg"] for l in received_logs]
        self.assertTrue(any("Uvicorn running" in m for m in messages))
        self.assertTrue(any("GET /api/airport" in m for m in messages))
        self.assertTrue(all(l["processName"] == "webview" for l in received_logs))

        logging.getLogger().removeHandler(ipc_handler)
        ipc_handler.close()
        ch_client.close()
        ch_master.close()

    def test_master_child_pipeline_logging_integration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "master.log"
            master = MasterProcess(
                log_file=log_file,
                enable_console_log=False,
            )
            master._create_pipes()

            for ch in master.master_channels.values():
                ch.start()

            ctrl_conn = master.pipes["master_ctrl_flight"][1]
            child_ch = IPCChannel(ctrl_conn, name="test_child_ctrl")
            child_handler = IPCLogHandler(channel=child_ch, process_name="agent_controller")

            child_logger = logging.getLogger("test_child_logger")
            child_logger.setLevel(logging.INFO)
            child_logger.addHandler(child_handler)
            child_logger.propagate = False

            child_logger.info("Test IPC message from controller to master!")

            # Poll for log file content to ensure async pipe delivery
            found = False
            for _ in range(40):
                time.sleep(0.05)
                master.file_handler.flush()
                if log_file.exists() and "Test IPC message from controller to master!" in log_file.read_text(encoding="utf-8"):
                    found = True
                    break

            self.assertTrue(found, "IPC log message was not delivered or recorded in master.log within timeout")

            child_logger.removeHandler(child_handler)
            child_handler.close()
            child_ch.close()
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
            master.file_handler.close()
            logging.getLogger().removeHandler(master.file_handler)


if __name__ == "__main__":
    unittest.main()
