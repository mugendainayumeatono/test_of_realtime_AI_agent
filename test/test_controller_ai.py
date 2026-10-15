import sys
import os
import asyncio
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from agent_controller import controller

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
        self.ctrl.get_objects = AsyncMock(return_value=[{"id": "1", "name": "Drone-1", "lat": 34.1, "lon": -118.1}])
        self.ctrl.send_radio_message = AsyncMock()
        
        # Mock the AI response
        mock_response = MagicMock()
        mock_response.text = "Drone-1, turn left heading 180"
        mock_generate_content.return_value = mock_response

        # Make sleep raise an exception to break the infinite loop after one iteration
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # Verify AI was called with the correct context
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1", prompt_arg)
        self.assertIn("34.0", prompt_arg)

        # Verify radio message was sent with AI response
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

        # AI must NOT be invoked and NO radio commands issued
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
        mock_response = MagicMock()
        mock_response.text = "Airborne-Drone, turn heading 090"
        mock_generate_content.return_value = mock_response
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await self.ctrl.run_loop()

        # AI must be called ONLY with the airborne aircraft
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Airborne-Drone", prompt_arg)
        self.assertNotIn("Grounded-Drone", prompt_arg)
        self.ctrl.send_radio_message.assert_called_once_with("Airborne-Drone, turn heading 090")

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

