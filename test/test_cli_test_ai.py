import sys
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add src, src/platform and cli to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'cli')))

import cli
from agent_common import ai_client
from webview import app as platform_app, webview_instance
from fastapi.testclient import TestClient


class TestCliTestAi(unittest.TestCase):
    def test_parser_test_ai(self):
        """Test build_parser parses test-ai and its aliases with all options"""
        parser = cli.build_parser()

        # test-ai default
        args = parser.parse_args(["test-ai"])
        self.assertEqual(args.command, "test-ai")
        self.assertEqual(args.target, "all")
        self.assertFalse(args.output_json)
        self.assertEqual(args.timeout, 30.0)

        # test-ai-endian alias
        args2 = parser.parse_args(["test-ai-endian", "--target", "controller", "--json"])
        self.assertEqual(args2.command, "test-ai-endian")
        self.assertEqual(args2.target, "controller")
        self.assertTrue(args2.output_json)

        # test_ai with pilot target and custom urls
        args3 = parser.parse_args([
            "test_ai",
            "--target", "pilot",
            "--controller-url", "http://127.0.0.1:8035",
            "--pilot-url", "http://127.0.0.1:8025",
            "--timeout", "5.0"
        ])
        self.assertEqual(args3.target, "pilot")
        self.assertEqual(args3.controller_url, "http://127.0.0.1:8035")
        self.assertEqual(args3.pilot_url, "http://127.0.0.1:8025")
        self.assertEqual(args3.timeout, 5.0)

    def test_parser_top_level_flags(self):
        """Test build_parser parses top-level flags --test-ai and --test-ai-endian"""
        parser = cli.build_parser()

        args = parser.parse_args(["--test-ai"])
        self.assertTrue(args.test_ai_flag)
        self.assertEqual(args.target, "all")
        self.assertEqual(args.timeout, 30.0)

        args2 = parser.parse_args(["--test-ai-endian", "--target", "pilot", "--json", "--timeout", "45.0"])
        self.assertTrue(args2.test_ai_flag)
        self.assertEqual(args2.target, "pilot")
        self.assertTrue(args2.output_json)
        self.assertEqual(args2.timeout, 45.0)

    @patch('cli.http_get')
    def test_test_controller_ai_endian_success(self, mock_http_get):
        """Test test_controller_ai_endian communicates solely via Webview proxy"""
        mock_http_get.return_value = {
            "status": "success",
            "available": True,
            "source": "agent_controller",
            "latency": 0.5,
            "response": "OK"
        }
        res = cli.test_controller_ai_endian(platform_url="http://localhost:8010", timeout=30.0)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["available"])
        self.assertIn("webview", res["channel"])
        mock_http_get.assert_called_once_with("http://localhost:8010/api/controller/test_ai?timeout=30.0", timeout=35.0)

    @patch('cli.http_get')
    def test_test_controller_ai_endian_no_direct_connection(self, mock_http_get):
        """Test test_controller_ai_endian never connects directly to controller port 8030"""
        mock_http_get.return_value = {
            "status": "success",
            "available": True,
            "source": "agent_controller",
        }
        # Even if legacy controller_url is passed, communication must go through platform_url
        cli.test_controller_ai_endian(
            platform_url="http://localhost:8010",
            controller_url="http://localhost:8030",
            timeout=20.0
        )
        called_url = mock_http_get.call_args[0][0]
        self.assertNotIn("8030", called_url)
        self.assertIn("http://localhost:8010/api/controller/test_ai?timeout=20.0", called_url)

    @patch('cli.http_get')
    def test_test_controller_ai_endian_failure(self, mock_http_get):
        """Test test_controller_ai_endian handles webview proxy error cleanly"""
        mock_http_get.side_effect = ConnectionRefusedError("Connection refused")

        res = cli.test_controller_ai_endian(
            platform_url="http://localhost:8010",
            timeout=30.0
        )
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["available"])
        self.assertEqual(res["channel"], "failed")
        self.assertIn("Webview proxy error", res["error"])

    @patch('cli.http_get')
    def test_test_pilot_ai_endian_success(self, mock_http_get):
        """Test test_pilot_ai_endian communicates solely via Webview proxy"""
        mock_http_get.return_value = {
            "status": "success",
            "available": True,
            "source": "agent_pilot",
            "latency": 0.3,
            "response": "OK"
        }
        res = cli.test_pilot_ai_endian(platform_url="http://localhost:8010", timeout=30.0)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["available"])
        self.assertIn("webview", res["channel"])
        mock_http_get.assert_called_once_with("http://localhost:8010/api/pilot/test_ai?timeout=30.0", timeout=35.0)

    @patch('cli.http_get')
    def test_test_pilot_ai_endian_no_direct_connection(self, mock_http_get):
        """Test test_pilot_ai_endian never connects directly to pilot port 8020"""
        mock_http_get.return_value = {
            "status": "success",
            "available": True,
            "source": "agent_pilot",
        }
        # Even if legacy pilot_url is passed, communication must go through platform_url
        cli.test_pilot_ai_endian(
            platform_url="http://localhost:8010",
            pilot_url="http://localhost:8020",
            timeout=20.0
        )
        called_url = mock_http_get.call_args[0][0]
        self.assertNotIn("8020", called_url)
        self.assertIn("http://localhost:8010/api/pilot/test_ai?timeout=20.0", called_url)

    @patch('cli.http_get')
    def test_test_pilot_ai_endian_failure(self, mock_http_get):
        """Test test_pilot_ai_endian handles webview proxy failure cleanly"""
        mock_http_get.side_effect = ConnectionRefusedError("Connection refused")

        res = cli.test_pilot_ai_endian(
            platform_url="http://localhost:8010",
            timeout=30.0
        )
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["available"])
        self.assertEqual(res["channel"], "failed")
        self.assertIn("Webview proxy error", res["error"])

    @patch('cli.test_controller_ai_endian')
    @patch('cli.test_pilot_ai_endian')
    def test_test_ai_endian_all(self, mock_pilot, mock_controller):
        """Test test_ai_endian with target='all' runs concurrently"""
        mock_controller.return_value = {"status": "success", "available": True}
        mock_pilot.return_value = {"status": "success", "available": True}

        results = cli.test_ai_endian(target="all", timeout=30.0)
        self.assertIn("controller", results)
        self.assertIn("pilot", results)
        mock_controller.assert_called_once()
        mock_pilot.assert_called_once()

    @patch('cli.test_controller_ai_endian')
    @patch('cli.test_pilot_ai_endian')
    def test_test_ai_endian_controller_only(self, mock_pilot, mock_controller):
        """Test test_ai_endian with target='controller'"""
        mock_controller.return_value = {"status": "success", "available": True}

        results = cli.test_ai_endian(target="controller")
        self.assertIn("controller", results)
        self.assertNotIn("pilot", results)
        mock_pilot.assert_not_called()

    @patch('cli.test_controller_ai_endian')
    @patch('cli.test_pilot_ai_endian')
    def test_test_ai_endian_pilot_only(self, mock_pilot, mock_controller):
        """Test test_ai_endian with target='pilot'"""
        mock_pilot.return_value = {"status": "success", "available": True}

        results = cli.test_ai_endian(target="pilot")
        self.assertNotIn("controller", results)
        self.assertIn("pilot", results)
        mock_controller.assert_not_called()

    def test_print_ai_test_results(self):
        """Test print_ai_test_results formats output without raising exceptions"""
        sample_results = {
            "controller": {
                "status": "success",
                "available": True,
                "latency": 0.52,
                "client_type": "openai",
                "channel": "webview proxy (http://localhost:8010)",
                "response": "OK"
            },
            "pilot": {
                "status": "error",
                "available": False,
                "latency": None,
                "client_type": None,
                "channel": "failed",
                "error": "Connection timed out after 30.0s"
            }
        }
        # Should execute cleanly and output error details
        cli.print_ai_test_results(sample_results)

    def test_unicode_cell_width_and_padding(self):
        """Test unicode cell width calculation and alignment"""
        w1 = cli._cell_width("ABC")
        self.assertEqual(w1, 3)
        w2 = cli._cell_width("Controller")
        self.assertEqual(w2, 10)
        padded = cli._pad_cell("Controller", 15)
        self.assertEqual(cli._cell_width(padded), 15)

    @patch('cli.test_ai_endian')
    def test_handle_test_ai_action_success(self, mock_test_ai):
        mock_test_ai.return_value = {
            "controller": {"status": "success", "available": True},
            "pilot": {"status": "success", "available": True}
        }
        ok = cli.handle_test_ai_action(target="all", platform_url="http://localhost:8010", output_json=True)
        self.assertTrue(ok)

    @patch('cli.test_ai_endian')
    def test_handle_test_ai_action_failure(self, mock_test_ai):
        mock_test_ai.return_value = {
            "controller": {"status": "error", "available": False},
            "pilot": {"status": "success", "available": True}
        }
        ok = cli.handle_test_ai_action(target="all", platform_url="http://localhost:8010", output_json=True)
        self.assertFalse(ok)


class TestAiClientConnectivity(unittest.TestCase):
    @patch('agent_common.ai_client.generate_ai_response')
    def test_check_ai_connectivity_success(self, mock_gen):
        mock_gen.return_value = "OK"
        res = ai_client.check_ai_connectivity()
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["available"])
        self.assertEqual(res["response"], "OK")
        self.assertIsInstance(res["latency"], float)

    @patch('agent_common.ai_client.generate_ai_response')
    def test_check_ai_connectivity_failure(self, mock_gen):
        mock_gen.side_effect = RuntimeError("API key invalid")
        res = ai_client.check_ai_connectivity()
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["available"])
        self.assertIn("API key invalid", res["error"])

    @patch('agent_common.ai_client.check_ai_connectivity')
    def test_test_ai_api_compat(self, mock_check):
        mock_check.return_value = {"available": True}
        self.assertTrue(ai_client.test_ai_api())
        mock_check.return_value = {"available": False}
        self.assertFalse(ai_client.test_ai_api())


class TestPlatformProxy(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(platform_app)
        self.mock_ctrl_ch = AsyncMock()
        self.mock_pilot_ch = AsyncMock()
        self.orig_ctrl_ch = webview_instance.ch_flight_ctrl
        self.orig_pilot_ch = webview_instance.ch_flight_pilot
        webview_instance.ch_flight_ctrl = self.mock_ctrl_ch
        webview_instance.ch_flight_pilot = self.mock_pilot_ch

    def tearDown(self):
        webview_instance.ch_flight_ctrl = self.orig_ctrl_ch
        webview_instance.ch_flight_pilot = self.orig_pilot_ch

    def test_proxy_controller_test_ai(self):
        self.mock_ctrl_ch.call.return_value = {
            "status": "success",
            "source": "agent_controller",
            "available": True
        }

        response = self.client.get("/api/controller/test_ai?timeout=25.0")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["source"], "agent_controller")
        self.mock_ctrl_ch.call.assert_called_once_with("test_ai", {"timeout": 25.0}, timeout=30.0)

    def test_proxy_pilot_test_ai(self):
        self.mock_pilot_ch.call.return_value = {
            "status": "success",
            "source": "agent_pilot",
            "available": True
        }

        response = self.client.get("/api/pilot/test_ai?timeout=25.0")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["source"], "agent_pilot")
        self.mock_pilot_ch.call.assert_called_once_with("test_ai", {"timeout": 25.0}, timeout=30.0)

    def test_proxy_pilot_radio_rx(self):
        self.mock_pilot_ch.call.return_value = {
            "status": "success",
            "resolved_count": 1,
            "resolved_ids": ["uuid-test"],
            "extracted": {"target": "TestDrone", "speed": 200}
        }

        response = self.client.post("/api/pilot/radio_RX", json={"text": "TestDrone speed up to 200"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["resolved_count"], 1)
        self.mock_pilot_ch.call.assert_called_once_with("radio_RX", {"text": "TestDrone speed up to 200"}, timeout=60.0)


if __name__ == '__main__':
    unittest.main()
