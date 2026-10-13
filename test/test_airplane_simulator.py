import os
import sys
import time
import unittest
from fastapi.testclient import TestClient

# Add src and src/platform to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'platform')))

from airplane_simulator import AirplaneSimulator
from webview import app, airplane_sim


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


class TestWebviewEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Ensure global simulator is stopped before each test
        airplane_sim.stop()
        airplane_sim.clear_objects()

    def tearDown(self):
        airplane_sim.stop()
        airplane_sim.clear_objects()

    def test_simulator_state_endpoints(self):
        # Initial state should be not running
        resp = self.client.get("/api/simulator/state")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["running"])

        # Start simulator via POST /api/simulator/state
        resp = self.client.post("/api/simulator/state", json={"running": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["running"])
        self.assertTrue(airplane_sim.is_running())

        # Check GET /api/simulator/state again
        resp = self.client.get("/api/simulator/state")
        self.assertTrue(resp.json()["running"])

        # Stop simulator via POST /api/simulator/stop
        resp = self.client.post("/api/simulator/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["running"])
        self.assertFalse(airplane_sim.is_running())

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


if __name__ == "__main__":
    unittest.main()

