import io
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add src, src/platform and cli to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'cli')))

import cli


class TestCliAirplane(unittest.TestCase):
    def test_parser_airplane_delete(self):
        """Test build_parser parses airplane delete subcommand with --id option."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete", "--id", "550e8400-e29b-41d4-a716-446655440000"])
        self.assertEqual(args.command, "airplane")
        self.assertEqual(args.airplane_action, "delete")
        self.assertEqual(args.id, "550e8400-e29b-41d4-a716-446655440000")

    def test_parser_airplane_delete_aliases(self):
        """Test build_parser supports del and remove aliases for delete."""
        parser = cli.build_parser()

        args_del = parser.parse_args(["airplane", "del", "--id", "uuid-111"])
        self.assertEqual(args_del.airplane_action, "del")
        self.assertEqual(args_del.id, "uuid-111")

        args_remove = parser.parse_args(["plane", "remove", "--id", "uuid-222"])
        self.assertEqual(args_remove.command, "plane")
        self.assertEqual(args_remove.airplane_action, "remove")
        self.assertEqual(args_remove.id, "uuid-222")

    def test_parser_airplane_delete_without_id(self):
        """Test parser allows parsing delete without id so cli can validate and output friendly error."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete"])
        self.assertEqual(args.airplane_action, "delete")
        self.assertIsNone(args.id)

    def test_delete_airplane_helper_success(self):
        """Test delete_airplane helper function calls /api/delete with encoded id."""
        with patch("cli.http_get") as mock_get:
            mock_get.return_value = {"status": "success", "message": "Object removed", "id": "test-id"}
            res = cli.delete_airplane("http://localhost:8010", "test-id")
            self.assertEqual(res["status"], "success")
            mock_get.assert_called_once_with("http://localhost:8010/api/delete?id=test-id")

    def test_delete_airplane_helper_error(self):
        """Test delete_airplane raises Exception when /api/delete request fails."""
        with patch("cli.http_get") as mock_get:
            mock_get.side_effect = Exception("Connection refused")
            with self.assertRaises(Exception) as cm:
                cli.delete_airplane("http://localhost:8010", "bad-id")
            self.assertIn("Connection refused", str(cm.exception))
            mock_get.assert_called_once_with("http://localhost:8010/api/delete?id=bad-id")

    def test_run_cli_args_mode_airplane_delete_success(self):
        """Test running airplane delete with valid --id via run_cli_args_mode."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete", "--id", "target-uuid"])

        with patch("cli.delete_airplane") as mock_delete, patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_delete.return_value = {"status": "success", "message": "Object target-uuid removed", "id": "target-uuid"}
            cli.run_cli_args_mode(args, "http://localhost:8010")
            output = mock_out.getvalue()
            self.assertIn("[Success] Aircraft deleted successfully:", output)
            self.assertIn("target-uuid", output)
            mock_delete.assert_called_once_with("http://localhost:8010", object_id="target-uuid")

    def test_run_cli_args_mode_airplane_delete_missing_id(self):
        """Test airplane delete command without --id prints error and exits with code 2."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete"])

        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                cli.run_cli_args_mode(args, "http://localhost:8010")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Missing parameter: delete requires --id to specify aircraft UUID!", mock_err.getvalue())

    def test_run_cli_args_mode_airplane_delete_empty_id(self):
        """Test airplane delete command with empty --id prints error and exits with code 2."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete", "--id", "   "])

        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                cli.run_cli_args_mode(args, "http://localhost:8010")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Missing parameter: delete requires --id to specify aircraft UUID!", mock_err.getvalue())

    def test_run_cli_args_mode_airplane_delete_backend_error(self):
        """Test airplane delete command when backend reports error exits with code 1."""
        parser = cli.build_parser()
        args = parser.parse_args(["airplane", "delete", "--id", "non-existent-uuid"])

        with patch("cli.delete_airplane") as mock_delete, patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            mock_delete.return_value = {"status": "error", "message": "Object with id non-existent-uuid not found"}
            with self.assertRaises(SystemExit) as cm:
                cli.run_cli_args_mode(args, "http://localhost:8010")
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Failed to delete aircraft: Object with id non-existent-uuid not found", mock_err.getvalue())

    def test_menu_airplane_delete_select_by_index(self):
        """Test interactive delete option selecting aircraft by numeric index 1."""
        mock_planes = [
            {"id": "uuid-plane-1", "name": "Drone-A", "lat": 10.0, "lon": 20.0, "altitude_ft": 3000.0, "heading": 90.0, "speed_knots": 150.0},
            {"id": "uuid-plane-2", "name": "Drone-B", "lat": 11.0, "lon": 21.0, "altitude_ft": 4000.0, "heading": 180.0, "speed_knots": 200.0},
        ]
        inputs = [
            "3",          # Select delete submenu
            "1",          # Select plane #1
            "",           # Press enter to continue
            "0",          # Return to main menu
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_del.return_value = {"status": "success", "id": "uuid-plane-1"}
            cli.menu_airplane("http://localhost:8010")

            mock_del.assert_called_once_with("http://localhost:8010", "uuid-plane-1")
            self.assertIn("Aircraft (ID: uuid-plane-1) deleted successfully", mock_out.getvalue())

    def test_menu_airplane_delete_select_by_uuid(self):
        """Test interactive delete option selecting aircraft by full UUID."""
        mock_planes = [
            {"id": "uuid-plane-1", "name": "Drone-A"},
            {"id": "uuid-plane-2", "name": "Drone-B"},
        ]
        inputs = [
            "delete",        # Select delete via textual command
            "uuid-plane-2",  # Enter full UUID
            "",              # Press enter to continue
            "0",             # Return to main menu
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del:
            mock_del.return_value = {"status": "success", "id": "uuid-plane-2"}
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_called_once_with("http://localhost:8010", "uuid-plane-2")

    def test_menu_airplane_delete_select_by_short_uuid(self):
        """Test interactive delete option selecting aircraft by short 8-char UUID prefix."""
        mock_planes = [
            {"id": "12345678-abcd-1111-2222-333344445555", "name": "Drone-A"},
        ]
        inputs = [
            "3",             # Select delete
            "12345678",      # 8-char UUID prefix
            "",              # Enter to continue
            "0",             # Exit
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del:
            mock_del.return_value = {"status": "success", "id": "12345678-abcd-1111-2222-333344445555"}
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_called_once_with("http://localhost:8010", "12345678-abcd-1111-2222-333344445555")

    def test_menu_airplane_delete_select_by_name(self):
        """Test interactive delete option selecting aircraft by callsign/name."""
        mock_planes = [
            {"id": "uuid-plane-alpha", "name": "AlphaFlight"},
        ]
        inputs = [
            "del",           # Select delete via alias
            "AlphaFlight",   # Aircraft name
            "",              # Enter to continue
            "0",             # Exit
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del:
            mock_del.return_value = {"status": "success", "id": "uuid-plane-alpha"}
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_called_once_with("http://localhost:8010", "uuid-plane-alpha")

    def test_menu_airplane_delete_cancel_with_zero(self):
        """Test entering 0 cancels deletion without invoking delete_airplane."""
        mock_planes = [
            {"id": "uuid-plane-1", "name": "Drone-A"},
        ]
        inputs = [
            "3",          # Select delete
            "0",          # Cancel by entering 0
            "",           # Enter to continue (if any) or straight to exit
            "0",          # Return to main menu
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("Operation cancelled", mock_out.getvalue())

    def test_menu_airplane_delete_no_active_aircraft(self):
        """Test interactive delete when no aircraft are in airspace."""
        inputs = [
            "3",          # Select delete
            "",           # Enter to continue prompt
            "0",          # Return
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=[]), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("No active aircraft found in airspace to delete", mock_out.getvalue())

    def test_menu_airplane_delete_target_not_found(self):
        """Test entering an invalid ID or index displays not found error."""
        mock_planes = [
            {"id": "uuid-plane-1", "name": "Drone-A"},
        ]
        inputs = [
            "3",          # Select delete
            "99",         # Non-existent index
            "",           # Enter to continue
            "0",          # Exit
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("Aircraft identified by '99' not found", mock_out.getvalue())

    def test_resolve_aircraft_selection_direct(self):
        """Direct unit test of resolve_aircraft_selection helper covering edge cases."""
        planes = [
            {"id": "550e8400-e29b-41d4-a716-446655440000", "name": "Drone-A"},
            {"id": "a1b2c3d4-e29b-41d4-a716-446655440001", "name": "Drone-B"},
        ]

        # Valid 1-N index
        uuid, err = cli.resolve_aircraft_selection(planes, "1")
        self.assertEqual(uuid, "550e8400-e29b-41d4-a716-446655440000")
        self.assertIsNone(err)

        uuid, err = cli.resolve_aircraft_selection(planes, "2")
        self.assertEqual(uuid, "a1b2c3d4-e29b-41d4-a716-446655440001")
        self.assertIsNone(err)

        # Cancellations
        for cancel_cmd in ("0", "00", "q", "quit", "cancel", "exit", "back", ""):
            uuid, err = cli.resolve_aircraft_selection(planes, cancel_cmd)
            self.assertIsNone(uuid)
            self.assertEqual(err, "cancelled")

        # Critical bug test: Out-of-bounds numeric index "5" must NOT match UUID prefix "5"!
        uuid, err = cli.resolve_aircraft_selection(planes, "5")
        self.assertIsNone(uuid)
        self.assertIn("index out of range", err)

        # Case-insensitive UUID match
        uuid, err = cli.resolve_aircraft_selection(planes, "550E8400-E29B-41D4-A716-446655440000")
        self.assertEqual(uuid, "550e8400-e29b-41d4-a716-446655440000")
        self.assertIsNone(err)

        # Case-insensitive callsign match
        uuid, err = cli.resolve_aircraft_selection(planes, "drone-a")
        self.assertEqual(uuid, "550e8400-e29b-41d4-a716-446655440000")
        self.assertIsNone(err)

        # Ambiguous prefix match
        planes_ambiguous = [
            {"id": "abcd-1111", "name": "P1"},
            {"id": "abcd-2222", "name": "P2"},
        ]
        uuid, err = cli.resolve_aircraft_selection(planes_ambiguous, "abcd")
        self.assertIsNone(uuid)
        self.assertIn("Ambiguous prefix", err)

    def test_menu_airplane_delete_out_of_range_index_does_not_delete_matching_prefix_plane(self):
        """Ensure entering index 5 when 2 planes exist does not delete plane 1 whose UUID starts with 5."""
        mock_planes = [
            {"id": "550e8400-e29b-41d4-a716-446655440000", "name": "Drone-A"},
            {"id": "a1b2c3d4-e29b-41d4-a716-446655440001", "name": "Drone-B"},
        ]
        inputs = [
            "3",          # Select delete
            "5",          # Out of bounds index
            "",           # Enter to continue
            "0",          # Exit
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("index out of range", mock_out.getvalue())

    def test_menu_airplane_delete_case_insensitive_uuid(self):
        """Test interactive delete selecting aircraft using uppercase UUID."""
        mock_planes = [
            {"id": "uuid-plane-lower", "name": "Drone-A"},
        ]
        inputs = [
            "3",
            "UUID-PLANE-LOWER",
            "",
            "0",
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del:
            mock_del.return_value = {"status": "success", "id": "uuid-plane-lower"}
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_called_once_with("http://localhost:8010", "uuid-plane-lower")

    def test_menu_airplane_delete_cancel_with_cancel_word(self):
        """Test typing 'cancel' or 'q' cancels interactive deletion without error."""
        mock_planes = [
            {"id": "uuid-1", "name": "Drone-A"},
        ]
        inputs = [
            "3",
            "cancel",
            "",
            "0",
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("Operation cancelled", mock_out.getvalue())

    def test_menu_airplane_delete_ambiguous_prefix(self):
        """Test entering an ambiguous prefix matching multiple aircraft prints error and prevents deletion."""
        mock_planes = [
            {"id": "test-uuid-alpha", "name": "Plane-1"},
            {"id": "test-uuid-beta", "name": "Plane-2"},
        ]
        inputs = [
            "3",
            "test-uuid",
            "",
            "0",
        ]

        with patch("builtins.input", side_effect=inputs), \
             patch("cli.get_airplanes", return_value=mock_planes), \
             patch("cli.delete_airplane") as mock_del, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            cli.menu_airplane("http://localhost:8010")
            mock_del.assert_not_called()
            self.assertIn("Ambiguous prefix", mock_out.getvalue())


if __name__ == "__main__":
    unittest.main()
