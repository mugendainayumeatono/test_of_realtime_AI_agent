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
    @patch('agent_controller.controller.test_ai_api')
    @patch('agent_controller.controller.get_controller_state', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_airport', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_objects', new_callable=AsyncMock)
    @patch('agent_controller.controller.send_radio_message', new_callable=AsyncMock)
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_ai_command(
        self, mock_sleep, mock_generate_content, mock_send_radio, 
        mock_get_objects, mock_get_airport, mock_get_controller_state, mock_test_ai_api
    ):
        # Setup mocks
        mock_get_controller_state.return_value = {"enabled": True}
        mock_get_airport.return_value = {"lat": 34.0, "lon": -118.0, "runway_heading": 180}
        mock_get_objects.return_value = [{"id": "1", "name": "Drone-1", "lat": 34.1, "lon": -118.1}]
        
        # Mock the AI response
        mock_response = MagicMock()
        mock_response.text = "Drone-1, turn left heading 180"
        mock_generate_content.return_value = mock_response

        # Make sleep raise an exception to break the infinite loop after one iteration
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await controller.run_loop()

        # Verify AI was called with the correct context
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1", prompt_arg)
        self.assertIn("34.0", prompt_arg)

        # Verify radio message was sent with AI response
        mock_send_radio.assert_called_once_with("Drone-1, turn left heading 180")

    @patch('agent_controller.controller.test_ai_api')
    @patch('agent_controller.controller.get_controller_state', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_airport', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_objects', new_callable=AsyncMock)
    @patch('agent_controller.controller.send_radio_message', new_callable=AsyncMock)
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_stops_dispatch_when_all_aircraft_on_ground(
        self, mock_sleep, mock_generate_content, mock_send_radio,
        mock_get_objects, mock_get_airport, mock_get_controller_state, mock_test_ai_api
    ):
        # All aircraft are in "ground" status
        mock_get_controller_state.return_value = {"enabled": True}
        mock_get_airport.return_value = {"lat": 34.0, "lon": -118.0, "runway_heading": 180}
        mock_get_objects.return_value = [
            {"id": "1", "name": "Grounded-1", "lat": 34.0, "lon": -118.0, "status": "ground"},
            {"id": "2", "name": "Grounded-2", "lat": 34.001, "lon": -118.001, "status": "ground"}
        ]
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await controller.run_loop()

        # AI must NOT be invoked and NO radio commands issued
        mock_generate_content.assert_not_called()
        mock_send_radio.assert_not_called()

    @patch('agent_controller.controller.test_ai_api')
    @patch('agent_controller.controller.get_controller_state', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_airport', new_callable=AsyncMock)
    @patch('agent_controller.controller.get_objects', new_callable=AsyncMock)
    @patch('agent_controller.controller.send_radio_message', new_callable=AsyncMock)
    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('asyncio.sleep', new_callable=AsyncMock)
    async def test_controller_dispatches_only_airborne_aircraft(
        self, mock_sleep, mock_generate_content, mock_send_radio,
        mock_get_objects, mock_get_airport, mock_get_controller_state, mock_test_ai_api
    ):
        # Mixed: 1 on ground, 1 in air
        mock_get_controller_state.return_value = {"enabled": True}
        mock_get_airport.return_value = {"lat": 34.0, "lon": -118.0, "runway_heading": 180}
        mock_get_objects.return_value = [
            {"id": "1", "name": "Grounded-Drone", "lat": 34.0, "lon": -118.0, "status": "ground"},
            {"id": "2", "name": "Airborne-Drone", "lat": 34.1, "lon": -118.1, "status": "airborne"}
        ]
        mock_response = MagicMock()
        mock_response.text = "Airborne-Drone, turn heading 090"
        mock_generate_content.return_value = mock_response
        mock_sleep.side_effect = LoopBreak("Break loop")

        with self.assertRaises(LoopBreak):
            await controller.run_loop()

        # AI must be called ONLY with the airborne aircraft
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Airborne-Drone", prompt_arg)
        self.assertNotIn("Grounded-Drone", prompt_arg)
        mock_send_radio.assert_called_once_with("Airborne-Drone, turn heading 090")


    @patch('agent_controller.controller.check_ai_connectivity')
    def test_controller_test_ai_endpoint(self, mock_check):
        from fastapi.testclient import TestClient
        client = TestClient(controller.app)
        mock_check.return_value = {
            "status": "success",
            "available": True,
            "client_type": "gemini",
            "latency": 0.45,
            "response": "OK"
        }
        response = client.get("/test_ai")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["source"], "agent_controller")
        self.assertTrue(data["available"])

