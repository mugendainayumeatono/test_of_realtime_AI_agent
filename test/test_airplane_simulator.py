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


if __name__ == "__main__":
    unittest.main()
