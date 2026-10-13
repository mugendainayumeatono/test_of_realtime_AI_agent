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
    def test_parser_state_save(self):
        """Test build_parser parses state save subcommand."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "save"])
        self.assertEqual(args.command, "state")
        self.assertEqual(args.state_action, "save")

    def test_parser_state_load(self):
        """Test build_parser parses state load / restore subcommands."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "load"])
        self.assertEqual(args.command, "state")
        self.assertEqual(args.state_action, "load")

        args_restore = parser.parse_args(["state", "restore"])
        self.assertEqual(args_restore.state_action, "restore")

    def test_parser_state_show(self):
        """Test build_parser parses state status / get / show subcommands."""
        parser = cli.build_parser()
        for act in ["status", "get", "show"]:
            args = parser.parse_args(["state", act])
            self.assertEqual(args.command, "state")
            self.assertEqual(args.state_action, act)

    def test_parser_state_aliases(self):
        """Test build_parser supports snapshot and scene aliases."""
        parser = cli.build_parser()
        args_snap = parser.parse_args(["snapshot", "save"])
        self.assertEqual(args_snap.command, "snapshot")
        self.assertEqual(args_snap.state_action, "save")

        args_scene = parser.parse_args(["scene", "load"])
        self.assertEqual(args_scene.command, "scene")
        self.assertEqual(args_scene.state_action, "load")

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

    def test_cli_args_mode_state_save(self):
        """Test run_cli_args_mode with state save command."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "save"])
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

    def test_cli_args_mode_state_load(self):
        """Test run_cli_args_mode with state load command."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "load"])
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

    def test_cli_args_mode_state_show(self):
        """Test run_cli_args_mode with state show command."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "show"])
        with patch("cli.get_state") as mock_get_state, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_get_state.return_value = {
                "status": "success",
                "airport": {"lat": 30.5, "lon": 114.3},
                "controller_enabled": True,
                "objects": [
                    {
                        "id": "u1",
                        "name": "Plane1",
                        "lat": 30.5,
                        "lon": 114.3,
                        "altitude_ft": 5000,
                        "speed_knots": 180,
                        "heading": 90,
                        "status": "airborne",
                    }
                ]
            }
            cli.run_cli_args_mode(args, "http://localhost:8010")
            out = mock_out.getvalue()
            self.assertIn("Current Airspace State Snapshot", out)
            self.assertIn("Plane1", out)
            mock_get_state.assert_called_once_with("http://localhost:8010")

    def test_legacy_save_action(self):
        """Test legacy single action 'save' calls save_state."""
        with patch.object(sys, "argv", ["cli.py", "save"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.save_state", return_value={"status": "success", "objects_count": 1}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Scene state successfully saved to state.json!", mock_out.getvalue())

    def test_legacy_load_action(self):
        """Test legacy single action 'load' calls load_state."""
        with patch.object(sys, "argv", ["cli.py", "load"]), \
             patch("cli.resolve_base_url", return_value="http://localhost:8010"), \
             patch("cli.load_state", return_value={"status": "success", "objects_count": 2}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Scene state successfully restored from state.json!", mock_out.getvalue())

    def test_parser_reset(self):
        """Test build_parser parses top-level reset and state reset subcommands."""
        parser = cli.build_parser()
        args_reset = parser.parse_args(["reset"])
        self.assertEqual(args_reset.command, "reset")

        args_state_reset = parser.parse_args(["state", "reset"])
        self.assertEqual(args_state_reset.command, "state")
        self.assertEqual(args_state_reset.state_action, "reset")

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

    def test_cli_args_mode_state_reset(self):
        """Test run_cli_args_mode with state reset command."""
        parser = cli.build_parser()
        args = parser.parse_args(["state", "reset"])
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
            self.assertIn("All aircraft deleted from simulator", out)
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
            self.assertIn("state save", out)
            self.assertIn("reset", out)

    def test_cli_subcommand_help(self):
        """Test cli.py <subcommand> --help outputs descriptions and specific examples."""
        for subcmd in ["airplane", "airport", "radio", "state", "reset", "simulator", "controller", "on-off"]:
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


