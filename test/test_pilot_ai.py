import sys
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from agent_pilot.pilot import AgentPilot

class TestPilotAI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pilot = AgentPilot()

    @patch('google.generativeai.GenerativeModel.generate_content')
    async def test_radio_rx_command(self, mock_generate_content):
        # Mock AI response to return a valid JSON object string
        mock_response = MagicMock()
        mock_response.text = '{"target": "Drone-1", "speed": 150, "heading": 180, "altitude": 2000}'
        mock_generate_content.return_value = mock_response

        # Mock simulator interactions directly on the pilot instance
        self.pilot.get_simulator_objects = AsyncMock(return_value=[{"id": "uuid-1234", "name": "Drone-1"}])
        self.pilot.update_simulator_object = AsyncMock(return_value={"status": "success", "object": {"id": "uuid-1234"}})

        # Handle radio RX command
        data = await self.pilot.handle_radio_command("Drone-1 speed up to 150, heading 180, altitude 2000")

        # Verify command execution
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["extracted"]["target"], "Drone-1")

        # Verify AI was called with the correct text
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1 speed up", prompt_arg)

    @patch('agent_pilot.pilot.check_ai_connectivity')
    async def test_test_ai_endpoint(self, mock_check):
        mock_check.return_value = {
            "status": "success",
            "available": True,
            "client_type": "gemini",
            "latency": 0.5,
            "response": "OK"
        }
        res = await self.pilot._ipc_handle_test_ai({})
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["source"], "agent_pilot")
        self.assertTrue(res["available"])


if __name__ == '__main__':
    unittest.main()

