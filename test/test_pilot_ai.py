import sys
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from agent_pilot.pilot import app

class TestPilotAI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch('google.generativeai.GenerativeModel.generate_content')
    @patch('httpx.AsyncClient')
    def test_radio_rx_command(self, mock_async_client_class, mock_generate_content):
        # Mock AI response to return a valid JSON object string
        mock_response = MagicMock()
        mock_response.text = '{"target": "Drone-1", "speed": 150, "heading": 180, "altitude": 2000}'
        mock_generate_content.return_value = mock_response
        
        # Mock AsyncClient context manager
        mock_client_instance = AsyncMock()
        mock_async_client_class.return_value.__aenter__.return_value = mock_client_instance
        
        # Set up get response
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = [{"id": "uuid-1234", "name": "Drone-1"}]
        mock_client_instance.get.return_value = mock_get_response
        
        # Perform request to the Pilot endpoint
        response = self.client.post("/radio_RX", json={"text": "Drone-1 speed up to 150, heading 180, altitude 2000"})
        
        # Verify HTTP status is OK
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["extracted"]["target"], "Drone-1")
        
        # Verify AI was called with the correct text
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1 speed up", prompt_arg)
