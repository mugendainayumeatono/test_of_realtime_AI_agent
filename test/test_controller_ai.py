import sys
import os
import json
import time
import asyncio
import multiprocessing
import unittest
from typing import List
from unittest.mock import patch, MagicMock, AsyncMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from agent_controller import controller
from agent_controller.controller import ScheduleItem
from agent_common.ipc import IPCChannel

class LoopBreak(Exception):
    pass

class TestControllerAI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctrl = controller.AgentController()

    @patch('agent_controller.controller.test_ai_api')
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_ai_command(
        self, mock_sleep, mock_generate_content, mock_test_ai_api
    ):
        # Setup mocks directly on the controller instance
        self.ctrl.get_controller_state = AsyncMock(return_value={"enabled": True})
        self.ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        self.ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "Drone-1", "lat": 34.1, "lon": -118.1, "status": "airborne"}])
        self.ctrl.send_radio_message = AsyncMock()
        
        # Mock structured JSON AI response
        mock_response = MagicMock()
        mock_response.text = json.dumps([
            {"id": "Drone-1", "is_schedule": True, "msg": "Drone-1, turn left heading 180"}
        ])
        mock_generate_content.return_value = mock_response

        # Make sleep raise an exception to break the infinite loop after one iteration
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # Verify AI was called with the serialized dictionary context and response_schema
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1", prompt_arg)
        self.assertIn("34.0", prompt_arg)
        self.assertNotIn("{flight_data}", prompt_arg)
        
        # Verify generation_config schema
        gen_config = mock_generate_content.call_args.kwargs.get("generation_config")
        self.assertIsNotNone(gen_config)
        self.assertEqual(gen_config.response_mime_type, "application/json")
        self.assertEqual(gen_config.response_schema, List[ScheduleItem])

        # Verify radio message was sent with scheduled instruction
        self.ctrl.send_radio_message.assert_called_once_with("Drone-1, turn left heading 180")

    @patch('agent_controller.controller.test_ai_api')
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_stops_dispatch_when_all_aircraft_on_ground(
        self, mock_sleep, mock_generate_content, mock_test_ai_api
    ):
        # All aircraft are in "ground" status
        self.ctrl.get_controller_state = AsyncMock(return_value={"enabled": True})
        self.ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        self.ctrl.get_objects = AsyncMock(return_value=[
            {"id": "1", "name": "Grounded-1", "lat": 34.0, "lon": -118.0, "status": "ground"},
            {"id": "2", "name": "Grounded-2", "lat": 34.001, "lon": -118.001, "status": "ground"}
        ])
        self.ctrl.send_radio_message = AsyncMock()
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # Since all aircraft are on the ground, AI is NOT called and NO radio commands issued
        mock_generate_content.assert_not_called()
        self.ctrl.send_radio_message.assert_not_called()

    @patch('agent_controller.controller.test_ai_api')
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_dispatches_only_airborne_aircraft(
        self, mock_sleep, mock_generate_content, mock_test_ai_api
    ):
        # Mixed: 1 on ground, 1 in air
        self.ctrl.get_controller_state = AsyncMock(return_value={"enabled": True})
        self.ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        self.ctrl.get_objects = AsyncMock(return_value=[
            {"id": "1", "name": "Grounded-Drone", "lat": 34.0, "lon": -118.0, "status": "ground"},
            {"id": "2", "name": "Airborne-Drone", "lat": 34.1, "lon": -118.1, "status": "airborne"}
        ])
        self.ctrl.send_radio_message = AsyncMock()

        # AI outputs is_schedule=True for Airborne-Drone
        mock_response = MagicMock()
        mock_response.text = json.dumps([
            {"id": "2", "is_schedule": True, "msg": "Airborne-Drone, turn heading 090"}
        ])
        mock_generate_content.return_value = mock_response
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # AI must be called ONLY with airborne aircraft; grounded aircraft is excluded
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Airborne-Drone", prompt_arg)
        self.assertNotIn("Grounded-Drone", prompt_arg)

        # Only the scheduled message should be dispatched
        self.ctrl.send_radio_message.assert_called_once_with("Airborne-Drone, turn heading 090")

    @patch('agent_controller.controller.test_ai_api')
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_concatenates_multiple_scheduled_aircraft(
        self, mock_sleep, mock_generate_content, mock_test_ai_api
    ):
        # Two aircraft both require instructions
        self.ctrl.get_controller_state = AsyncMock(return_value={"enabled": True})
        self.ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        self.ctrl.get_objects = AsyncMock(return_value=[
            {"id": "JA-1", "name": "JA-1", "lat": 34.1, "lon": -118.1, "status": "airborne"},
            {"id": "JA-2", "name": "JA-2", "lat": 34.2, "lon": -118.2, "status": "airborne"}
        ])
        self.ctrl.send_radio_message = AsyncMock()

        mock_response = MagicMock()
        mock_response.text = json.dumps([
            {"id": "JA-1", "is_schedule": True, "msg": "JA-1, turn left heading 180"},
            {"id": "JA-2", "is_schedule": True, "msg": "JA-2, descend to 2000 feet"}
        ])
        mock_generate_content.return_value = mock_response
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # Instructions should be concatenated together with comma
        self.ctrl.send_radio_message.assert_called_once_with(
            "JA-1, turn left heading 180, JA-2, descend to 2000 feet"
        )

    @patch('agent_controller.controller.test_ai_api')
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_empty_objects_does_not_call_ai(
        self, mock_sleep, mock_generate_content, mock_test_ai_api
    ):
        self.ctrl.get_controller_state = AsyncMock(return_value={"enabled": True})
        self.ctrl.get_objects = AsyncMock(return_value=[])
        self.ctrl.send_radio_message = AsyncMock()
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        mock_generate_content.assert_not_called()
        self.ctrl.send_radio_message.assert_not_called()

    def test_controller_parse_schedule_response(self):
        # 1. Valid JSON array with is_schedule
        raw = json.dumps([
            {"id": "A1", "is_schedule": True, "msg": "climb 3000"},
            {"id": "A2", "is_schedule": False, "msg": ""}
        ])
        items = self.ctrl.parse_schedule_response(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].id, "A1")
        self.assertTrue(items[0].is_schedule)
        self.assertEqual(items[0].msg, "climb 3000")
        self.assertFalse(items[1].is_schedule)

        # 2. Empty input
        self.assertEqual(self.ctrl.parse_schedule_response(""), [])

        # 3. Non-JSON text should log error and return empty list
        with self.assertLogs("controller", level="ERROR") as cm:
            items_err = self.ctrl.parse_schedule_response("This is not JSON text")
            self.assertEqual(items_err, [])
            self.assertTrue(any("Failed to parse AI schedule response as JSON" in msg for msg in cm.output))

    @patch('agent_controller.controller.check_ai_connectivity')
    async def test_controller_test_ai_ipc_handler(self, mock_check):
        mock_check.return_value = {
            "status": "success",
            "available": True,
            "client_type": "gemini",
            "latency": 0.45,
            "response": "OK"
        }
        res = await self.ctrl._ipc_handle_test_ai({})
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["source"], "agent_controller")
        self.assertTrue(res["available"])

    def test_controller_initialization_state_and_logging(self):
        with self.assertLogs("controller", level="INFO") as cm:
            ctrl = controller.AgentController()
        self.assertFalse(ctrl.enabled)
        self.assertTrue(any("Initialized controller_state to enabled=False on startup" in msg for msg in cm.output))

    async def test_controller_ipc_responsive_during_slow_ai(self):
        """关键测试：当 AI 生成响应耗时较长时，IPC 管道事件循环绝不阻塞，get_controller_state 必须秒级立即响应"""
        wv_conn, ctrl_conn = multiprocessing.Pipe(duplex=True)
        ctrl = controller.AgentController(conn_flight_webview=ctrl_conn)
        ch_wv = IPCChannel(wv_conn, name="test_wv->ctrl")

        cur_loop = asyncio.get_running_loop()
        ch_wv.start(loop=cur_loop)
        for ch in ctrl._get_active_channels():
            ch.start(loop=cur_loop)

        ctrl.set_controller_state(True)
        ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "JA-Slow", "status": "airborne"}])
        ctrl.send_radio_message = AsyncMock()

        # 模拟 AI 接口网络延迟 0.3 秒
        def slow_ai_response(*args, **kwargs):
            time.sleep(0.3)
            return json.dumps([{"id": "1", "is_schedule": True, "msg": "JA-Slow, climb 5000"}])

        orig_sleep = asyncio.sleep
        async def mock_sleep_fn(seconds, *args, **kwargs):
            if seconds >= 1.0:
                raise LoopBreak("Break after 1 iteration")
            return await orig_sleep(seconds, *args, **kwargs)

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", side_effect=slow_ai_response), \
             patch("agent_controller.controller.generate_ai_response_with_history", side_effect=slow_ai_response), \
             patch("asyncio.sleep", side_effect=mock_sleep_fn):

            loop_task = asyncio.create_task(ctrl.run_loop())

            # 等待 0.05 秒确保 run_loop 已经进入 slow_ai_response
            await asyncio.sleep(0.05)

            # 在 AI 阻塞调用的同时，发起 IPC get_controller_state 请求
            t0 = time.time()
            res = await ch_wv.call("get_controller_state", timeout=1.0)
            elapsed = time.time() - t0

            # 必须在 < 0.15s 极短时间内立即响应，绝不能等待 AI 完成的 0.3 秒或超时
            self.assertLess(elapsed, 0.15)
            self.assertTrue(res.get("enabled", False))

            try:
                await loop_task
            except LoopBreak:
                pass

        ch_wv.close()
        ctrl.stop()

    async def test_controller_discards_command_if_disabled_during_ai(self):
        """测试：如果在 AI 计算耗时期间用户关闭了管制开关，AI 算出的旧指令必须被安全丢弃，不得发送无线电广播"""
        ctrl = controller.AgentController()
        ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "JA-Test", "status": "airborne"}])
        ctrl.send_radio_message = AsyncMock()

        def slow_ai_response(*args, **kwargs):
            time.sleep(0.1)
            return json.dumps([{"id": "1", "is_schedule": True, "msg": "JA-Test, climb 5000"}])

        ctrl.set_controller_state(True)

        orig_sleep = asyncio.sleep
        async def mock_sleep_fn(seconds, *args, **kwargs):
            if seconds >= 1.0:
                raise LoopBreak("Break loop")
            return await orig_sleep(seconds, *args, **kwargs)

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", side_effect=slow_ai_response), \
             patch("agent_controller.controller.generate_ai_response_with_history", side_effect=slow_ai_response), \
             patch("asyncio.sleep", side_effect=mock_sleep_fn):

            loop_task = asyncio.create_task(ctrl.run_loop())

            await asyncio.sleep(0.03)
            # 在 AI 计算中途将管制员关闭
            ctrl.set_controller_state(False)

            try:
                await loop_task
            except LoopBreak:
                pass

            # 无线电广播必须不被调用
            ctrl.send_radio_message.assert_not_called()

    async def test_controller_ai_network_failure_resilience(self):
        """测试：当 AI 接口抛出网络异常（如 502 Bad Gateway / 超时）时，run_loop 优雅容错记录日志，不崩溃也不阻塞循环"""
        ctrl = controller.AgentController()
        ctrl.set_controller_state(True)
        ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "JA-Test", "status": "airborne"}])
        ctrl.send_radio_message = AsyncMock()

        def failing_ai_response(*args, **kwargs):
            raise RuntimeError("502 Bad Gateway from AI proxy")

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", side_effect=failing_ai_response), \
             patch("agent_controller.controller.generate_ai_response_with_history", side_effect=failing_ai_response), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             self.assertLogs("controller", level="ERROR") as cm:

            mock_sleep.side_effect = LoopBreak("Break after error caught")
            with self.assertRaises(LoopBreak):
                await ctrl.run_loop()

            ctrl.send_radio_message.assert_not_called()
            self.assertTrue(any("502 Bad Gateway" in msg for msg in cm.output))

    async def test_controller_multi_turn_with_history_interface(self):
        """测试：当配置 use_history=True 时，管制员第1轮发送替换占位符后的系统提示词，后续轮次复用会话且仅发送更新后的飞行状态"""
        ctrl = controller.AgentController(use_history=True)
        ctrl.set_controller_state(True)
        ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        ctrl.get_objects = AsyncMock(side_effect=[
            [{"id": "1", "name": "JA-101", "lat": 34.2, "lon": -118.2, "altitude": 4000, "status": "airborne"}],
            [{"id": "1", "name": "JA-101", "lat": 34.1, "lon": -118.1, "altitude": 3000, "status": "airborne"}],
        ])
        ctrl.send_radio_message = AsyncMock()

        mock_client = MagicMock()
        r1 = MagicMock()
        r1.choices = [MagicMock()]
        r1.choices[0].message.content = json.dumps([
            {"id": "JA-101", "is_schedule": True, "msg": "JA-101, descend and maintain 3000 feet"}
        ])
        r2 = MagicMock()
        r2.choices = [MagicMock()]
        r2.choices[0].message.content = json.dumps([
            {"id": "JA-101", "is_schedule": True, "msg": "JA-101, cleared ILS approach"}
        ])
        mock_client.chat.completions.create.side_effect = [r1, r2]

        sleep_calls = 0
        async def sleep_two_turns(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise LoopBreak("Done 2 turns")

        with patch("agent_controller.controller.get_ai_model", return_value=mock_client), \
             patch("agent_common.ai_client.AI_CLIENT_TYPE", "openai"), \
             patch("agent_common.ai_client.get_ai_model", return_value=mock_client), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("asyncio.sleep", side_effect=sleep_two_turns):
            with self.assertRaises(LoopBreak):
                await ctrl.run_loop()

        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

        # 第1轮：仅包含1条 role="system" 消息（提示词模板已替换 {flight_data}）
        turn1_msgs = mock_client.chat.completions.create.call_args_list[0].kwargs["messages"]
        self.assertEqual(len(turn1_msgs), 1)
        self.assertEqual(turn1_msgs[0]["role"], "system")
        self.assertIn("JA-101", turn1_msgs[0]["content"])
        self.assertIn("4000", turn1_msgs[0]["content"])
        self.assertNotIn("{flight_data}", turn1_msgs[0]["content"])

        # 第2轮：包含历史记录 [system, assistant] + 当前轮次 [user]，且 user 消息仅含飞行状态而不重复包含提示词模板
        turn2_msgs = mock_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        self.assertEqual([m["role"] for m in turn2_msgs], ["system", "assistant", "user"])
        user_msg = turn2_msgs[2]["content"]
        parsed_user_json = json.loads(user_msg)
        self.assertIn("airport", parsed_user_json)
        self.assertIn("aircraft", parsed_user_json)
        self.assertEqual(parsed_user_json["aircraft"][0]["altitude"], 3000)

    async def test_controller_with_history_disabled_uses_stateless_interface(self):
        """测试：当配置 use_history=False 时，管制员使用旧的无状态 generate_ai_response 接口"""
        ctrl = controller.AgentController(use_history=False)
        ctrl.set_controller_state(True)
        ctrl.get_airport = AsyncMock(return_value={"lat": 34.0, "lon": -118.0, "runway_heading": 180})
        ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "JA-Legacy", "status": "airborne"}])
        ctrl.send_radio_message = AsyncMock()

        with patch("agent_controller.controller.get_ai_model", return_value=True), \
             patch("agent_controller.controller.test_ai_api"), \
             patch("agent_controller.controller.generate_ai_response", return_value=json.dumps([{"id": "JA-Legacy", "is_schedule": True, "msg": "JA-Legacy, turn heading 180"}])) as mock_legacy, \
             patch("agent_controller.controller.generate_ai_response_with_history") as mock_history, \
             patch("asyncio.sleep", side_effect=LoopBreak("Break")):
            with self.assertRaises(LoopBreak):
                await ctrl.run_loop()

        mock_legacy.assert_called_once()
        mock_history.assert_not_called()
        ctrl.send_radio_message.assert_called_once_with("JA-Legacy, turn heading 180")


if __name__ == "__main__":
    unittest.main()
