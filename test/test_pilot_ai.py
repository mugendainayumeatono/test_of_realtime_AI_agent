import asyncio
import json
import multiprocessing
import os
import sys
import time
import unittest
from typing import List
from unittest.mock import patch, MagicMock, AsyncMock

# Add src and src/platform to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))

from agent_pilot.pilot import AgentPilot, PilotCommandItem
from agent_common.ipc import IPCChannel
from airplane_simulator import AirplaneSimulatorService


class TestPilotAI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pilot = AgentPilot()

    @patch('google.generativeai.GenerativeModel.generate_content')
    async def test_radio_rx_command_structured_output_with_vertical_speed_and_turn_rate(self, mock_generate_content):
        # Mock AI response returning structured JSON array with vertical_speed_ft_min and turn_rate_deg_sec
        mock_response = MagicMock()
        mock_response.text = json.dumps([
            {
                "target": "Drone-1",
                "speed": 150,
                "heading": 180,
                "altitude": 2000,
                "vertical_speed_ft_min": 800,
                "turn_rate_deg_sec": 2.5,
            }
        ])
        mock_generate_content.return_value = mock_response

        # Mock simulator interactions directly on the pilot instance
        self.pilot.get_simulator_objects = AsyncMock(return_value=[{"id": "uuid-1234", "name": "Drone-1"}])
        self.pilot.update_simulator_object = AsyncMock(return_value={"status": "success", "object": {"id": "uuid-1234"}})

        # Handle radio RX command
        data = await self.pilot.handle_radio_command(
            "Drone-1 speed up to 150, heading 180 at 2.5 degrees per second, descend to 2000 at 800 feet per minute"
        )

        # Verify command execution
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["resolved_count"], 1)
        self.assertEqual(data["resolved_ids"], ["uuid-1234"])
        self.assertNotIn("extracted", data)
        self.assertEqual(len(data["extracted_items"]), 1)
        self.assertEqual(data["extracted_items"][0]["target"], "Drone-1")
        self.assertEqual(data["extracted_items"][0]["speed"], 150)
        self.assertEqual(data["extracted_items"][0]["heading"], 180)
        self.assertEqual(data["extracted_items"][0]["altitude"], 2000)
        self.assertEqual(data["extracted_items"][0]["vertical_speed_ft_min"], 800)
        self.assertEqual(data["extracted_items"][0]["turn_rate_deg_sec"], 2.5)

        # Verify AI was called with the correct prompt and structured response_schema
        mock_generate_content.assert_called_once()
        prompt_arg = mock_generate_content.call_args[0][0]
        self.assertIn("Drone-1 speed up", prompt_arg)

        gen_config = mock_generate_content.call_args.kwargs.get("generation_config")
        self.assertIsNotNone(gen_config)
        self.assertEqual(gen_config.response_mime_type, "application/json")
        self.assertEqual(gen_config.response_schema, List[PilotCommandItem])

        # Verify update_simulator_object received vertical_speed_ft_min and turn_rate_deg_sec
        self.pilot.update_simulator_object.assert_called_once_with(
            "uuid-1234",
            {
                "speed": 150,
                "heading": 180,
                "altitude": 2000,
                "vertical_speed_ft_min": 800,
                "turn_rate_deg_sec": 2.5,
            }
        )

    @patch('google.generativeai.GenerativeModel.generate_content')
    async def test_multi_aircraft_control_in_single_radio_command(self, mock_generate_content):
        """测试：大模型一次返回多架飞机的结构化控制请求，Pilot 逐架分别通知模拟器更新"""
        mock_response = MagicMock()
        mock_response.text = json.dumps([
            {
                "target": "JA-NRT-4000",
                "speed": 140,
                "heading": 157,
                "altitude": 0,
                "vertical_speed_ft_min": 1200,
                "turn_rate_deg_sec": 3.0,
            },
            {
                "target": "JA-NRT-4001",
                "speed": 180,
                "heading": 337,
                "altitude": 1500,
                "vertical_speed_ft_min": 600,
                "turn_rate_deg_sec": 2.0,
            }
        ])
        mock_generate_content.return_value = mock_response

        self.pilot.get_simulator_objects = AsyncMock(return_value=[
            {"id": "uuid-4000", "name": "JA-NRT-4000"},
            {"id": "uuid-4001", "name": "JA-NRT-4001"},
            {"id": "uuid-other", "name": "Other-Plane"},
        ])
        self.pilot.update_simulator_object = AsyncMock(
            side_effect=lambda uid, params: {"status": "success", "object": {"id": uid, **params}}
        )

        radio_text = (
            "JA-NRT-4000, turn right heading 157 at 3 deg/s, descend to 0 feet at 1200 fpm, speed 140 knots, "
            "JA-NRT-4001, turn left heading 337 at 2 deg/s, descend to 1500 feet at 600 fpm, speed 180 knots"
        )
        data = await self.pilot.handle_radio_command(radio_text)

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["resolved_count"], 2)
        self.assertEqual(data["resolved_ids"], ["uuid-4000", "uuid-4001"])
        self.assertEqual(len(data["extracted_items"]), 2)
        self.assertEqual(len(data["platform_responses"]), 2)

        # Verify simulator was notified separately for each aircraft
        self.assertEqual(self.pilot.update_simulator_object.call_count, 2)
        calls = self.pilot.update_simulator_object.call_args_list
        self.assertEqual(calls[0][0][0], "uuid-4000")
        self.assertEqual(calls[0][0][1], {
            "speed": 140,
            "heading": 157,
            "altitude": 0,
            "vertical_speed_ft_min": 1200,
            "turn_rate_deg_sec": 3,
        })
        self.assertEqual(calls[1][0][0], "uuid-4001")
        self.assertEqual(calls[1][0][1], {
            "speed": 180,
            "heading": 337,
            "altitude": 1500,
            "vertical_speed_ft_min": 600,
            "turn_rate_deg_sec": 2,
        })

    async def test_multi_aircraft_end_to_end_with_simulator_ipc(self):
        """端到端 IPC 集成验证：Pilot 解析多架飞机控制参数（含垂直速度与转弯率）并真实更新 Simulator"""
        sim_conn, pilot_conn = multiprocessing.Pipe(duplex=True)
        sim_service = AirplaneSimulatorService(conn_flight_pilot=sim_conn)
        sim_service.start()

        pilot = AgentPilot(conn_flight_simulator=pilot_conn)
        pilot.start(loop=asyncio.get_running_loop())

        try:
            p1 = sim_service.simulator.add_object(name="Plane-A", lat=35.5, lon=140.3, speed=220.0, heading=360.0, altitude=4000.0)
            p2 = sim_service.simulator.add_object(name="Plane-B", lat=35.6, lon=140.4, speed=200.0, heading=90.0, altitude=3000.0)

            ai_json = json.dumps([
                {
                    "target": "Plane-A",
                    "speed": 140,
                    "heading": 157,
                    "altitude": 0,
                    "vertical_speed_ft_min": 1000,
                    "turn_rate_deg_sec": 4.5,
                },
                {
                    "target": "Plane-B",
                    "speed": 160,
                    "heading": 67,
                    "altitude": 1500,
                    "vertical_speed_ft_min": 500,
                    "turn_rate_deg_sec": 2.0,
                }
            ])

            with patch("agent_pilot.pilot.get_ai_model", return_value=True), \
                 patch("agent_pilot.pilot.generate_ai_response", return_value=ai_json):
                res = await pilot.handle_radio_command("Combined ATC command for Plane-A and Plane-B")

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["resolved_count"], 2)

            obj_a = sim_service.simulator.get_object(p1["id"])
            self.assertEqual(obj_a["speed_knots"], 140.0)
            self.assertEqual(obj_a["target_heading"], 157.0)
            self.assertEqual(obj_a["target_altitude_ft"], 0.0)
            self.assertEqual(obj_a["vertical_speed_ft_min"], 1000.0)
            self.assertEqual(obj_a["turn_rate_deg_sec"], 4.5)

            obj_b = sim_service.simulator.get_object(p2["id"])
            self.assertEqual(obj_b["speed_knots"], 160.0)
            self.assertEqual(obj_b["target_heading"], 67.0)
            self.assertEqual(obj_b["target_altitude_ft"], 1500.0)
            self.assertEqual(obj_b["vertical_speed_ft_min"], 500.0)
            self.assertEqual(obj_b["turn_rate_deg_sec"], 2.0)
        finally:
            pilot.stop()
            sim_service.stop()

    def test_pilot_parse_command_response(self):
        # 1. Valid JSON array with multiple aircraft
        raw = json.dumps([
            {"target": "A1", "speed": 180, "heading": 90, "altitude": 3000, "vertical_speed_ft_min": 750, "turn_rate_deg_sec": 3.0},
            {"target": "A2", "speed": None, "heading": 270, "altitude": 1500, "vertical_speed_ft_min": None, "turn_rate_deg_sec": 1.5},
        ])
        items = self.pilot.parse_command_response(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].target, "A1")
        self.assertEqual(items[0].vertical_speed_ft_min, 750.0)
        self.assertEqual(items[0].turn_rate_deg_sec, 3.0)
        self.assertEqual(items[1].target, "A2")
        self.assertEqual(items[1].turn_rate_deg_sec, 1.5)

        # 2. Single JSON object compatibility
        single_raw = json.dumps({"target": "Single-1", "speed": 200, "vertical_speed_ft_min": 600, "turn_rate_deg_sec": 2.0})
        items_single = self.pilot.parse_command_response(single_raw)
        self.assertEqual(len(items_single), 1)
        self.assertEqual(items_single[0].target, "Single-1")
        self.assertEqual(items_single[0].vertical_speed_ft_min, 600.0)
        self.assertEqual(items_single[0].turn_rate_deg_sec, 2.0)

        # 3. Empty input
        self.assertEqual(self.pilot.parse_command_response(""), [])

        # 4. Non-JSON text should log error and return empty list
        with self.assertLogs("pilot", level="ERROR") as cm:
            items_err = self.pilot.parse_command_response("Invalid non-JSON output")
            self.assertEqual(items_err, [])
            self.assertTrue(any("Failed to parse AI pilot response as JSON" in msg for msg in cm.output))

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
