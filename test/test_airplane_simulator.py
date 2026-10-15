import json
import os
import sys
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

# Add src and src/platform to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))

import multiprocessing
from airplane_simulator import AirplaneSimulator, AirplaneSimulatorService
from agent_common.ipc import IPCChannel
from webview import app, webview_instance


class TestAirplaneSimulator(unittest.TestCase):
    def setUp(self):
        self.sim = AirplaneSimulator()

    def tearDown(self):
        if self.sim.is_running():
            self.sim.stop()

    def test_thread_lifecycle_and_name(self):
        self.assertFalse(self.sim.is_running())
        started = self.sim.start()
        self.assertTrue(started)
        self.assertTrue(self.sim.is_running())
        
        # Verify thread name is airplane_simulator
        self.assertEqual(self.sim._thread.name, "airplane_simulator")
        
        # Calling start again while running should return False
        self.assertFalse(self.sim.start())
        
        # Stop the simulator
        stopped = self.sim.stop()
        self.assertTrue(stopped)
        self.assertFalse(self.sim.is_running())
        
        # Calling stop again should return False
        self.assertFalse(self.sim.stop())

    def test_object_crud_and_motion_state(self):
        # Add object
        obj = self.sim.add_object(
            name="TestDrone",
            lat=34.0,
            lon=-118.0,
            speed=120.0,
            heading=90.0,
            altitude=5000.0
        )
        self.assertIsNotNone(obj.get("id"))
        self.assertEqual(obj["name"], "TestDrone")
        self.assertEqual(obj["speed_knots"], 120.0)
        self.assertEqual(obj["heading"], 90.0)
        self.assertEqual(obj["altitude_ft"], 5000.0)

        # Query object and motion state
        obj_id = obj["id"]
        fetched = self.sim.get_object(obj_id)
        self.assertEqual(fetched["name"], "TestDrone")

        motion_state = self.sim.get_motion_state()
        self.assertEqual(len(motion_state), 1)
        self.assertEqual(motion_state[0]["id"], obj_id)

        # Update motion parameters
        self.sim.set_speed(obj_id, 200.0)
        self.sim.set_heading(obj_id, 180.0, as_target=True)
        self.sim.set_altitude(obj_id, 10000.0, as_target=True)

        updated = self.sim.get_object(obj_id)
        self.assertEqual(updated["speed_knots"], 200.0)
        self.assertEqual(updated["target_heading"], 180.0)
        self.assertEqual(updated["target_altitude_ft"], 10000.0)

        # Remove object
        removed = self.sim.remove_object(obj_id)
        self.assertTrue(removed)
        self.assertIsNone(self.sim.get_object(obj_id))
        self.assertEqual(len(self.sim.get_objects()), 0)

    def test_simulation_movement(self):
        obj = self.sim.add_object(
            name="Flyer",
            lat=0.0,
            lon=0.0,
            speed=3600.0, # High speed for noticeable displacement
            heading=90.0,
            altitude=1000.0
        )
        obj_id = obj["id"]

        # Before start, position remains fixed
        time.sleep(0.6)
        self.assertEqual(self.sim.get_object(obj_id)["lon"], 0.0)

        # Start simulation loop
        self.sim.start()
        time.sleep(1.2)
        self.sim.stop()

        moved_obj = self.sim.get_object(obj_id)
        # Heading 90 means moving eastward, lon should increase
        self.assertGreater(moved_obj["lon"], 0.0)

    def test_ground_and_airborne_status_calculation(self):
        # Configure airport
        self.sim.set_airport(lat=34.0, lon=-118.0, runway_heading=90.0)

        # 1. Close to airport (0 NM <= 1 NM) and altitude < 100 ft -> ground
        ground_plane = self.sim.add_object(
            name="GroundPlane",
            lat=34.0,
            lon=-118.0,
            speed=50.0,
            altitude=50.0
        )
        self.assertEqual(ground_plane["status"], "ground")
        self.assertEqual(ground_plane["speed_knots"], 0.0)
        self.assertEqual(ground_plane["vertical_speed_ft_min"], 0.0)

        # 2. Close to airport (0 NM) but altitude >= 100 ft -> airborne
        air_plane = self.sim.add_object(
            name="AirPlane",
            lat=34.0,
            lon=-118.0,
            speed=120.0,
            altitude=500.0
        )
        self.assertEqual(air_plane["status"], "airborne")
        self.assertEqual(air_plane["speed_knots"], 120.0)

        # 3. Far from airport (> 1 NM) even if altitude < 100 ft -> airborne
        far_plane = self.sim.add_object(
            name="FarPlane",
            lat=35.0,  # 1 degree lat is ~60 NM away
            lon=-118.0,
            speed=100.0,
            altitude=50.0
        )
        self.assertEqual(far_plane["status"], "airborne")
        self.assertEqual(far_plane["speed_knots"], 100.0)

    def test_airborne_to_ground_transition_resets_speed_and_vertical_rate(self):
        self.sim.set_airport(lat=34.0, lon=-118.0)

        # Initially in air near airport
        plane = self.sim.add_object(
            name="LandingPlane",
            lat=34.0,
            lon=-118.0,
            speed=140.0,
            altitude=300.0,
            vertical_speed_ft_min=-600.0
        )
        pid = plane["id"]
        self.assertEqual(plane["status"], "airborne")
        self.assertEqual(plane["speed_knots"], 140.0)

        # Descend below 100 ft (e.g. 50 ft)
        found, updated = self.sim.update_object(pid, altitude_ft=50.0)
        self.assertTrue(found)
        self.assertEqual(updated["status"], "ground")
        # Speed and vertical speed must be automatically reset to 0
        self.assertEqual(updated["speed_knots"], 0.0)
        self.assertEqual(updated["vertical_speed_ft_min"], 0.0)

    def test_ground_to_airborne_transition(self):
        self.sim.set_airport(lat=34.0, lon=-118.0)

        plane = self.sim.add_object(
            name="DepartingPlane",
            lat=34.0,
            lon=-118.0,
            altitude=0.0
        )
        pid = plane["id"]
        self.assertEqual(self.sim.get_object(pid)["status"], "ground")

        # Climb to 500 ft (takes off)
        found, updated = self.sim.update_object(pid, altitude_ft=500.0)
        self.assertTrue(found)
        self.assertEqual(updated["status"], "airborne")

    def test_simulation_step_landing_trigger(self):
        self.sim.set_airport(lat=34.0, lon=-118.0)

        # Aircraft descending towards target altitude 0 ft
        plane = self.sim.add_object(
            name="FinalApproach",
            lat=34.0,
            lon=-118.0,
            speed=100.0,
            altitude=120.0,
            target_altitude=0.0,
            vertical_speed_ft_min=-600.0
        )
        pid = plane["id"]
        self.assertEqual(self.sim.get_object(pid)["status"], "airborne")

        # Step 5 seconds: descent = 600 ft/min * 5/60 min = 50 ft => altitude becomes 70 ft (<100 ft)
        self.sim.step(5.0)

        stepped = self.sim.get_object(pid)
        self.assertLess(stepped["altitude_ft"], 100.0)
        self.assertEqual(stepped["status"], "ground")
        self.assertEqual(stepped["speed_knots"], 0.0)
        self.assertEqual(stepped["vertical_speed_ft_min"], 0.0)

    def test_restore_objects(self):
        # Setup airport
        self.sim.set_airport(lat=34.0, lon=-118.0, runway_heading=270.0)

        # Pre-existing object that should be cleared on restore
        self.sim.add_object(name="OldObject", lat=10.0, lon=10.0)
        self.assertEqual(len(self.sim.get_objects()), 1)

        # Objects list to restore
        restore_payload = [
            {
                "id": "plane-ground-1",
                "name": "GroundPlane",
                "lat": 34.0,
                "lon": -118.0,
                "heading": 270.0,
                "speed_knots": 0.0,
                "altitude_ft": 0.0,
                "vertical_speed_ft_min": 0.0,
                "turn_rate_deg_sec": 3.0,
                "target_heading": 270.0,
                "target_altitude_ft": 0.0,
                "status": "ground"
            },
            {
                "id": "plane-air-2",
                "name": "AirPlane",
                "lat": 35.0,
                "lon": -117.0,
                "heading": 180.0,
                "speed_knots": 240.0,
                "altitude_ft": 8000.0,
                "vertical_speed_ft_min": 500.0,
                "turn_rate_deg_sec": 2.5,
                "target_heading": 180.0,
                "target_altitude_ft": 10000.0,
                "status": "airborne"
            }
        ]

        restored = self.sim.restore_objects(restore_payload)
        self.assertEqual(len(restored), 2)
        self.assertEqual(len(self.sim.get_objects()), 2)

        p1 = self.sim.get_object("plane-ground-1")
        self.assertIsNotNone(p1)
        self.assertEqual(p1["name"], "GroundPlane")
        self.assertEqual(p1["status"], "ground")
        self.assertEqual(p1["speed_knots"], 0.0)

        p2 = self.sim.get_object("plane-air-2")
        self.assertIsNotNone(p2)
        self.assertEqual(p2["name"], "AirPlane")
        self.assertEqual(p2["status"], "airborne")
        self.assertEqual(p2["speed_knots"], 240.0)
        self.assertEqual(p2["altitude_ft"], 8000.0)


class TestWebviewEndpoints(unittest.TestCase):
    def setUp(self):
        self.wv_pipe, self.sim_pipe = multiprocessing.Pipe(duplex=True)
        self.sim_service = AirplaneSimulatorService(conn_flight_webview=self.sim_pipe)
        self.sim_service.start()

        webview_instance.conn_flight_sim = self.wv_pipe
        webview_instance.ch_flight_sim = IPCChannel(self.wv_pipe, name="test_wv->sim")
        webview_instance.ch_flight_sim.start()

        self.client = TestClient(app)

    def tearDown(self):
        self.sim_service.stop()
        if webview_instance.ch_flight_sim:
            webview_instance.ch_flight_sim.close()
            webview_instance.ch_flight_sim = None
            webview_instance.conn_flight_sim = None

    def test_simulator_state_endpoints(self):
        # Initial state should be not running
        resp = self.client.get("/api/simulator/state")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["running"])

        # Start simulator via POST /api/simulator/state
        resp = self.client.post("/api/simulator/state", json={"running": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["running"])
        self.assertTrue(self.sim_service.simulator.is_running())

        # Check GET /api/simulator/state again
        resp = self.client.get("/api/simulator/state")
        self.assertTrue(resp.json()["running"])

        # Stop simulator via POST /api/simulator/stop
        resp = self.client.post("/api/simulator/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["running"])
        self.assertFalse(self.sim_service.simulator.is_running())

    def test_objects_api_integration(self):
        # Add object via /api/add
        resp = self.client.get("/api/add?name=APITest&speed=150&heading=270&altitude=3000")
        self.assertEqual(resp.status_code, 200)
        obj_id = resp.json()["object"]["id"]

        # Read objects via /api/objects
        resp = self.client.get("/api/objects")
        self.assertEqual(resp.status_code, 200)
        objects = resp.json()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["name"], "APITest")

        # Update object via /api/update
        resp = self.client.get(f"/api/update?id={obj_id}&speed=250&heading=360&altitude=6000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

        # Verify updated in /api/objects
        resp = self.client.get("/api/objects")
        updated_obj = resp.json()[0]
        self.assertEqual(updated_obj["speed_knots"], 250.0)
        self.assertEqual(updated_obj["target_heading"], 0.0)
        self.assertEqual(updated_obj["target_altitude_ft"], 6000.0)

    def test_delete_endpoint(self):
        # Add 3 objects
        resp1 = self.client.get("/api/add?name=Plane1")
        id1 = resp1.json()["object"]["id"]
        resp2 = self.client.get("/api/add?name=Plane2")
        id2 = resp2.json()["object"]["id"]
        resp3 = self.client.get("/api/add?name=Plane3")
        id3 = resp3.json()["object"]["id"]

        self.assertEqual(len(self.client.get("/api/objects").json()), 3)

        # 1. Delete id1 via GET /api/delete?id=...
        del_resp1 = self.client.get(f"/api/delete?id={id1}")
        self.assertEqual(del_resp1.status_code, 200)
        self.assertEqual(del_resp1.json()["status"], "success")
        self.assertEqual(del_resp1.json()["id"], id1)
        self.assertEqual(len(self.client.get("/api/objects").json()), 2)

        # 2. Delete id2 via GET /api/delete?id=...
        del_resp2 = self.client.get(f"/api/delete?id={id2}")
        self.assertEqual(del_resp2.status_code, 200)
        self.assertEqual(del_resp2.json()["status"], "success")
        self.assertEqual(del_resp2.json()["id"], id2)
        self.assertEqual(len(self.client.get("/api/objects").json()), 1)

        # 3. Delete id3 via POST /api/delete with JSON body
        del_resp3 = self.client.post("/api/delete", json={"id": id3})
        self.assertEqual(del_resp3.status_code, 200)
        self.assertEqual(del_resp3.json()["status"], "success")
        self.assertEqual(del_resp3.json()["id"], id3)
        self.assertEqual(len(self.client.get("/api/objects").json()), 0)

        # 4. Attempt deleting non-existent ID
        del_err = self.client.get("/api/delete?id=nonexistent")
        self.assertEqual(del_err.status_code, 200)
        self.assertEqual(del_err.json()["status"], "error")
        self.assertIn("not found", del_err.json()["message"])

        # 5. Attempt deleting with empty ID
        del_empty = self.client.get("/api/delete?id=")
        self.assertEqual(del_empty.status_code, 200)
        self.assertEqual(del_empty.json()["status"], "error")
        self.assertIn("Missing 'id'", del_empty.json()["message"])

        # 6. Test DELETE method on /api/delete
        resp4 = self.client.get("/api/add?name=Plane4")
        id4 = resp4.json()["object"]["id"]
        del_method = self.client.delete(f"/api/delete?id={id4}")
        self.assertEqual(del_method.status_code, 200)
        self.assertEqual(del_method.json()["status"], "success")
        self.assertEqual(len(self.client.get("/api/objects").json()), 0)

        # Verify /api/remove route is removed (returns 404)
        del_removed_route = self.client.get(f"/api/remove?id={id4}")
        self.assertEqual(del_removed_route.status_code, 404)

        # 7. Test DELETE method with JSON body payload on /api/delete
        resp5 = self.client.get("/api/add?name=Plane5")
        id5 = resp5.json()["object"]["id"]
        del_json_resp = self.client.request("DELETE", "/api/delete", json={"id": id5})
        self.assertEqual(del_json_resp.status_code, 200)
        self.assertEqual(del_json_resp.json()["status"], "success")
        self.assertEqual(len(self.client.get("/api/objects").json()), 0)

        # 8. Test DELETE /api/delete with uppercase UUID
        resp6 = self.client.get("/api/add?name=Plane6")
        id6 = resp6.json()["object"]["id"]
        del_upper_resp = self.client.get(f"/api/delete?id={id6.upper()}")
        self.assertEqual(del_upper_resp.status_code, 200)
        self.assertEqual(del_upper_resp.json()["status"], "success")
        self.assertEqual(len(self.client.get("/api/objects").json()), 0)

        # 9. Test POST /api/delete with query string parameter
        resp7 = self.client.get("/api/add?name=Plane7")
        id7 = resp7.json()["object"]["id"]
        del_post_query = self.client.post(f"/api/delete?id={id7}")
        self.assertEqual(del_post_query.status_code, 200)
        self.assertEqual(del_post_query.json()["status"], "success")
        self.assertEqual(len(self.client.get("/api/objects").json()), 0)

    def test_airport_sync_and_object_status_via_api(self):
        # 1. Set airport via POST /api/airport
        resp = self.client.post("/api/airport", json={"lat": 34.0, "lon": -118.0, "runway_heading": 270.0})
        self.assertEqual(resp.status_code, 200)

        # 2. Add object on the ground at airport (lat=34.0, lon=-118.0, altitude=0)
        add_resp = self.client.get("/api/add?name=RampPlane&lat=34.0&lon=-118.0&altitude=0.0&speed=20.0")
        self.assertEqual(add_resp.status_code, 200)
        plane = add_resp.json()["object"]
        self.assertEqual(plane["status"], "ground")
        self.assertEqual(plane["speed_knots"], 0.0)

        # 3. Add object in the air (altitude=2000)
        add_resp2 = self.client.get("/api/add?name=AirApproach&lat=34.0&lon=-118.0&altitude=2000.0&speed=150.0")
        self.assertEqual(add_resp2.status_code, 200)
        plane2 = add_resp2.json()["object"]
        self.assertEqual(plane2["status"], "airborne")
        self.assertEqual(plane2["speed_knots"], 150.0)

    def test_scene_state_persistence_save_and_load(self):
        from webview import STATE_FILE
        backup_content = None
        if STATE_FILE.exists():
            backup_content = STATE_FILE.read_text(encoding="utf-8")

        try:
            # 1. Set airport and add aircraft
            set_port = self.client.post("/api/airport", json={"lat": 31.2, "lon": 121.5, "runway_heading": 180.0})
            self.assertEqual(set_port.status_code, 200)

            add1 = self.client.get("/api/add?name=Airbus101&lat=31.2&lon=121.5&altitude=0&speed=0")
            self.assertEqual(add1.status_code, 200)
            id1 = add1.json()["object"]["id"]

            add2 = self.client.get("/api/add?name=Boeing202&lat=32.0&lon=122.0&altitude=7000&speed=260")
            self.assertEqual(add2.status_code, 200)
            id2 = add2.json()["object"]["id"]

            # 2. Save state via POST /api/state/save
            save_resp = self.client.post("/api/state/save")
            self.assertEqual(save_resp.status_code, 200)
            save_data = save_resp.json()
            self.assertEqual(save_data["status"], "success")
            self.assertEqual(save_data["objects_count"], 2)
            self.assertEqual(save_data["airport"]["lat"], 31.2)

            # Check file on disk
            self.assertTrue(STATE_FILE.exists())
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                disk_state = json.load(f)
            self.assertEqual(disk_state["airport"]["lat"], 31.2)
            self.assertEqual(len(disk_state["objects"]), 2)

            # 3. Clear objects and change airport to simulate state change/loss
            self.client.get(f"/api/delete?id={id1}")
            self.client.get(f"/api/delete?id={id2}")
            self.assertEqual(len(self.client.get("/api/objects").json()), 0)
            self.client.post("/api/airport", json={"lat": 0.0, "lon": 0.0, "runway_heading": 0.0})

            # 4. Restore state via POST /api/state/load
            load_resp = self.client.post("/api/state/load")
            self.assertEqual(load_resp.status_code, 200)
            load_data = load_resp.json()
            self.assertEqual(load_data["status"], "success")
            self.assertEqual(load_data["objects_count"], 2)
            self.assertEqual(load_data["airport"]["lat"], 31.2)

            # 5. Verify restored state in memory via GET endpoints
            objs_resp = self.client.get("/api/objects")
            objs = objs_resp.json()
            self.assertEqual(len(objs), 2)
            obj_names = {o["name"] for o in objs}
            self.assertIn("Airbus101", obj_names)
            self.assertIn("Boeing202", obj_names)

            port_resp = self.client.get("/api/airport")
            self.assertEqual(port_resp.json()["lat"], 31.2)

            state_resp = self.client.get("/api/state")
            self.assertEqual(state_resp.json()["objects_count"], 2)
            self.assertEqual(state_resp.json()["airport"]["lat"], 31.2)
        finally:
            if backup_content is not None:
                STATE_FILE.write_text(backup_content, encoding="utf-8")
            elif STATE_FILE.exists():
                STATE_FILE.unlink()

    def test_startup_does_not_auto_load_state_json(self):
        from webview import WebviewApp
        from agent_controller.controller import AgentController
        new_app = WebviewApp()
        # Verify initial clean state on new instance (airport is None)
        self.assertIsNone(new_app.current_airport.get("lat"))
        self.assertIsNone(new_app.current_airport.get("lon"))

        # Verify controller initializes to disabled state on startup
        new_ctrl = AgentController()
        self.assertFalse(new_ctrl.enabled)

    def test_api_reset_endpoint(self):
        from webview import webview_instance

        mock_ctrl_state = {"enabled": False}
        async def fake_get():
            return {"enabled": mock_ctrl_state["enabled"]}
        async def fake_set(enabled):
            mock_ctrl_state["enabled"] = bool(enabled)
            return {"status": "success", "enabled": mock_ctrl_state["enabled"]}

        with patch.object(webview_instance, "get_controller_state_via_ipc", side_effect=fake_get), \
             patch.object(webview_instance, "set_controller_state_via_ipc", side_effect=fake_set):
            # 1. Setup airport, aircraft, simulator and controller
            self.client.post("/api/airport", json={"lat": 34.05, "lon": -118.25, "runway_heading": 260.0})
            self.client.get("/api/add?name=ResetTestJet&lat=34.05&lon=-118.25&altitude=5000&speed=200")
            self.client.post("/api/simulator/start")
            self.client.post("/api/controller_state", json={"enabled": True})

            # Verify active scene before reset
            self.assertTrue(len(self.client.get("/api/objects").json()) > 0)
            self.assertIsNotNone(self.client.get("/api/airport").json().get("lat"))
            self.assertTrue(self.client.get("/api/simulator/state").json()["running"])
            self.assertTrue(self.client.get("/api/controller_state").json()["enabled"])

            # 2. Call POST /api/reset
            resp = self.client.post("/api/reset")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["objects_count"], 0)
            self.assertEqual(data["objects"], [])
            self.assertIsNone(data["airport"]["lat"])
            self.assertFalse(data["simulator_running"])
            self.assertFalse(data["controller_enabled"])
            self.assertFalse(mock_ctrl_state["enabled"])

            # 3. Verify simulator and controller states are cleared and stopped
            self.assertEqual(len(self.client.get("/api/objects").json()), 0)
            self.assertIsNone(self.client.get("/api/airport").json().get("lat"))
            self.assertFalse(self.client.get("/api/simulator/state").json()["running"])
            self.assertFalse(self.client.get("/api/controller_state").json()["enabled"])


if __name__ == "__main__":
    unittest.main()

