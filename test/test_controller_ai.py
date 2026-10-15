import sys
import os
import json
import asyncio
import unittest
from typing import List
from unittest.mock import patch, MagicMock, AsyncMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from agent_controller import controller
from agent_controller.controller import ScheduleItem

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

        # 2. Markdown wrapped JSON
        md_raw = f"```json\n{raw}\n```"
        items_md = self.ctrl.parse_schedule_response(md_raw)
        self.assertEqual(len(items_md), 2)

        # 3. Empty input
        self.assertEqual(self.ctrl.parse_schedule_response(""), [])

        # 4. Non-JSON text should log error and return empty list
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


if __name__ == "__main__":
    unittest.main()
