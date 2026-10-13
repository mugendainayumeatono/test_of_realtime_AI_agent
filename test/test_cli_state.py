import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add src, src/platform and cli to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'cli')))

import cli


class TestCliState(unittest.TestCase):
    def test_parser_save(self):
        """Test build_parser parses save subcommand."""
        parser = cli.build_parser()
        args = parser.parse_args(["save"])
        self.assertEqual(args.command, "save")
        self.assertFalse(args.output_json)

        args_json = parser.parse_args(["save", "--json"])
        self.assertEqual(args_json.command, "save")
        self.assertTrue(args_json.output_json)

    def test_parser_load(self):
        """Test build_parser parses load subcommand."""
        parser = cli.build_parser()
        args = parser.parse_args(["load"])
        self.assertEqual(args.command, "load")
        self.assertFalse(args.output_json)

        args_json = parser.parse_args(["load", "--json"])
        self.assertEqual(args_json.command, "load")
        self.assertTrue(args_json.output_json)

    def test_parser_state(self):
        """Test build_parser parses state subcommand (without subaction, for displaying flight state)."""
        parser = cli.build_parser()
        args = parser.parse_args(["state"])
        self.assertEqual(args.command, "state")
        self.assertFalse(args.output_json)
        self.assertFalse(args.watch)
        self.assertEqual(args.interval, 5.0)
        self.assertIsNone(args.count)

        args_json = parser.parse_args(["state", "--json"])
        self.assertEqual(args_json.command, "state")
        self.assertTrue(args_json.output_json)

        args_watch = parser.parse_args(["state", "--watch", "--interval", "2.5", "--count", "5"])
        self.assertTrue(args_watch.watch)
        self.assertEqual(args_watch.interval, 2.5)
        self.assertEqual(args_watch.count, 5)

        args_short = parser.parse_args(["state", "-w"])
        self.assertTrue(args_short.watch)
        self.assertEqual(args_short.interval, 5.0)

    def test_parser_state_rejects_legacy_actions(self):
        """Test state subcommand does not accept legacy subactions (save, load, reset)."""
        parser = cli.build_parser()
        for invalid_act in ["save", "load", "reset", "show", "status"]:
            with self.assertRaises(SystemExit):
                with patch("sys.stderr", new_callable=io.StringIO):
                    parser.parse_args(["state", invalid_act])

    def test_save_state_helper(self):
        """Test save_state helper function calls /api/state/save via HTTP POST."""
        with patch("cli.http_post") as mock_post:
            mock_post.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "objects_count": 3,
                "objects": []
            }
            res = cli.save_state("http://localhost:8010")
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["objects_count"], 3)
            mock_post.assert_called_once_with("http://localhost:8010/api/state/save", {})

    def test_load_state_helper(self):
        """Test load_state helper function calls /api/state/load via HTTP POST."""
        with patch("cli.http_post") as mock_post:
            mock_post.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "objects_count": 3,
                "objects": []
            }
            res = cli.load_state("http://localhost:8010")
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["objects_count"], 3)
            mock_post.assert_called_once_with("http://localhost:8010/api/state/load", {})

    def test_get_state_helper(self):
        """Test get_state helper function calls /api/state via HTTP GET."""
        with patch("cli.http_get") as mock_get:
            mock_get.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "objects_count": 1,
                "objects": [],
                "controller_enabled": False
            }
            res = cli.get_state("http://localhost:8010")
            self.assertEqual(res["status"], "success")
            mock_get.assert_called_once_with("http://localhost:8010/api/state")

    def test_cli_args_mode_save(self):
        """Test run_cli_args_mode with save command."""
        parser = cli.build_parser()
        args = parser.parse_args(["save"])
        with patch("cli.save_state") as mock_save, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_save.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "objects_count": 2,
            }
            cli.run_cli_args_mode(args, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Scene state successfully saved to state.json!", out)
            self.assertIn("Aircraft saved: 2", out)
            mock_save.assert_called_once_with("http://localhost:8010")

    def test_cli_args_mode_load(self):
        """Test run_cli_args_mode with load command."""
        parser = cli.build_parser()
        args = parser.parse_args(["load"])
        with patch("cli.load_state") as mock_load, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_load.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "objects_count": 2,
            }
            cli.run_cli_args_mode(args, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Scene state successfully restored from state.json!", out)
            self.assertIn("Aircraft restored: 2", out)
            mock_load.assert_called_once_with("http://localhost:8010")

    def test_cli_args_mode_state(self):
        """Test run_cli_args_mode with state command (displays flight state snapshot)."""
        parser = cli.build_parser()
        args = parser.parse_args(["state"])
        with patch("cli.get_state") as mock_get_state, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_get_state.return_value = {
                "status": "success",
                "airport": {"lat": 35.76, "lon": 140.38},
                "controller_enabled": True,
                "objects": [
                    {
                        "id": "u1",
                        "name": "NRT-Air",
                        "lat": 35.76,
                        "lon": 140.38,
                        "altitude_ft": 4000,
                        "speed_knots": 220,
                        "heading": 157,
                        "status": "airborne",
                    }
                ]
            }
            cli.run_cli_args_mode(args, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Current Airspace Flight State Snapshot", out)
            self.assertIn("NRT-Air", out)
            mock_get_state.assert_called_once_with("http://localhost:8010")

    def test_cli_args_mode_state_watch(self):
        """Test run_cli_args_mode with state --watch delegates to monitor_airspace_state."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "--watch", "--interval", "3.0", "--count", "4"])
        with patch("cli.monitor_airspace_state") as mock_monitor:
            cli.run_cli_args_mode(args, "http://localhost:8010")
            mock_monitor.assert_called_once_with("http://localhost:8010", interval=3.0, count=4)

    def test_print_airspace_state(self):
        """Test print_airspace_state prints formatted airport, controller, and airplanes table."""
        sample_res = {
            "status": "success",
            "airport": {"lat": 35.76, "lon": 140.38, "runway_heading": 157.0},
            "controller_enabled": True,
            "objects": [
                {
                    "id": "abc12345-6789-abcd-ef01-234567890abc",
                    "name": "NRT-Jet",
                    "lat": 35.80,
                    "lon": 140.40,
                    "altitude_ft": 4000.0,
                    "target_altitude_ft": 5000.0,
                    "heading": 180.0,
                    "target_heading": 200.0,
                    "speed_knots": 250.0,
                    "status": "airborne",
                }
            ],
        }
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.print_airspace_state(sample_res, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Platform URL: http://localhost:8010", out)
            self.assertIn("Airport: Lat 35.76, Lon 140.38, Runway Heading 157.0°", out)
            self.assertIn("Controller Enabled: True", out)
            self.assertIn("NRT-Jet", out)
            self.assertIn("abc12345", out)

    def test_monitor_airspace_state_count(self):
        """Test monitor_airspace_state executes specified count of refresh cycles."""
        sample_res = {
            "status": "success",
            "airport": {"lat": 35.76, "lon": 140.38},
            "controller_enabled": False,
            "objects": [],
        }
        with patch("cli.get_state", return_value=sample_res) as mock_get_state, \
             patch("time.sleep") as mock_sleep, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.monitor_airspace_state("http://localhost:8010", interval=0.1, count=2)
            self.assertEqual(mock_get_state.call_count, 2)
            mock_sleep.assert_called_once_with(0.1)
            out = mock_out.getvalue()
            self.assertIn("Airspace State Monitor Started", out)
            self.assertIn("Airspace Flight State Snapshot:", out)

    def test_monitor_airspace_state_keyboard_interrupt(self):
        """Test monitor_airspace_state exits gracefully upon KeyboardInterrupt."""
        sample_res = {
            "status": "success",
            "airport": None,
            "controller_enabled": False,
            "objects": [],
        }
        with patch("cli.get_state", return_value=sample_res), \
             patch("time.sleep", side_effect=KeyboardInterrupt), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.monitor_airspace_state("http://localhost:8010", interval=5.0)
            out = mock_out.getvalue()
            self.assertIn("Airspace State Monitor Started", out)
            self.assertIn("Airspace State Monitor Stopped", out)

    def test_menu_state_invokes_monitor(self):
        """Test menu_state in interactive mode calls monitor_airspace_state with 5s interval."""
        with patch("cli.monitor_airspace_state") as mock_monitor, \
             patch("builtins.input", return_value=""):
            cli.menu_state("http://localhost:8010")
            mock_monitor.assert_called_once_with("http://localhost:8010", interval=5.0)

    def test_menu_state_handles_keyboard_interrupt_on_input(self):
        """Test menu_state handles KeyboardInterrupt gracefully during return prompt."""
        with patch("cli.monitor_airspace_state"), \
             patch("builtins.input", side_effect=KeyboardInterrupt):
            cli.menu_state("http://localhost:8010")

    def test_legacy_save_action(self):
        """Test single action 'save' calls save_state."""
        with patch.object(sys, "argv", ["cli.py", "save"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.save_state", return_value={"status": "success", "objects_count": 1}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Scene state successfully saved to state.json!", mock_out.getvalue())

    def test_legacy_load_action(self):
        """Test single action 'load' calls load_state."""
        with patch.object(sys, "argv", ["cli.py", "load"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.load_state", return_value={"status": "success", "objects_count": 2}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Scene state successfully restored from state.json!", mock_out.getvalue())

    def test_legacy_state_action(self):
        """Test single action 'state' calls get_state and displays flight state snapshot."""
        with patch.object(sys, "argv", ["cli.py", "state"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.get_state", return_value={
                 "status": "success",
                 "airport": {"lat": 35.76, "lon": 140.38},
                 "controller_enabled": True,
                 "objects": [{"id": "p1", "name": "FlightState1", "lat": 35.76, "lon": 140.38, "altitude_ft": 4000, "speed_knots": 220, "heading": 157, "status": "airborne"}]
             }), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Current Airspace Flight State Snapshot", mock_out.getvalue())
            self.assertIn("FlightState1", mock_out.getvalue())

    def test_parser_reset(self):
        """Test build_parser parses top-level reset subcommand."""
        parser = cli.build_parser()
        args_reset = parser.parse_args(["reset"])
        self.assertEqual(args_reset.command, "reset")

    def test_reset_system_helper(self):
        """Test reset_system helper function calls /api/reset via HTTP POST."""
        with patch("cli.http_post") as mock_post:
            mock_post.return_value = {
                "status": "success",
                "message": "Airspace reset successfully",
                "airport": {"lat": None, "lon": None, "runway_heading": None},
                "objects_count": 0,
                "objects": [],
                "simulator_running": False,
                "controller_enabled": False,
            }
            res = cli.reset_system("http://localhost:8010")
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["objects_count"], 0)
            mock_post.assert_called_once_with("http://localhost:8010/api/reset", {})

    def test_cli_args_mode_reset(self):
        """Test run_cli_args_mode with top-level reset command."""
        parser = cli.build_parser()
        args = parser.parse_args(["reset"])
        with patch("cli.reset_system") as mock_reset, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_reset.return_value = {
                "status": "success",
                "airport": {"lat": None, "lon": None, "runway_heading": None},
                "objects_count": 0,
                "simulator_running": False,
                "controller_enabled": False,
            }
            cli.run_cli_args_mode(args, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Airspace reset successfully!", out)
            self.assertIn("Flight Simulator stopped", out)
            self.assertIn("AI Controller stopped", out)
            mock_reset.assert_called_once_with("http://localhost:8010")

    def test_legacy_reset_action(self):
        """Test legacy single action 'reset' calls reset_system."""
        with patch.object(sys, "argv", ["cli.py", "reset"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.reset_system", return_value={"status": "success"}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Airspace reset successfully", mock_out.getvalue())

    def test_cli_help_flag(self):
        """Test cli.py --help outputs comprehensive guide with examples."""
        with patch.object(sys, "argv", ["cli.py", "--help"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            out = mock_out.getvalue()
            self.assertIn("Realtime Aviation Multi-Agent System", out)
            self.assertIn("Detailed Command Reference & Examples:", out)
            self.assertIn("airplane add", out)
            self.assertIn("airport set", out)
            self.assertIn("test-ai", out)
            self.assertIn("radio", out)
            self.assertIn("state", out)
            self.assertIn("save", out)
            self.assertIn("load", out)
            self.assertIn("reset", out)

    def test_cli_subcommand_help(self):
        """Test cli.py <subcommand> --help outputs descriptions and specific examples."""
        for subcmd in ["airplane", "airport", "radio", "state", "save", "load", "reset", "simulator", "controller", "on-off"]:
            with patch.object(sys, "argv", ["cli.py", subcmd, "--help"]), \
                 patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                with self.assertRaises(SystemExit) as cm:
                    cli.main()
                self.assertEqual(cm.exception.code, 0)
                out = mock_out.getvalue()
                self.assertIn("Examples:", out)

    def test_cli_help_subcommand(self):
        """Test cli.py help and cli.py help <command> syntax."""
        with patch.object(sys, "argv", ["cli.py", "help"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            out = mock_out.getvalue()
            self.assertIn("Detailed Command Reference & Examples:", mock_out.getvalue())

        with patch.object(sys, "argv", ["cli.py", "help", "airplane", "add"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            out = mock_out.getvalue()
            self.assertIn("Add a new aircraft entity", out)
            self.assertIn("Examples:", out)
            self.assertIn("--random-pos", out)


if __name__ == "__main__":
    unittest.main()
