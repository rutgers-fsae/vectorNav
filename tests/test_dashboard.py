import json
from pathlib import Path
import tempfile
import asyncio
import unittest

from vectornav_dashboard.auth import create_auth_file, verify_pin
from vectornav_dashboard.coordinates import local_xy
from vectornav_dashboard.server import DashboardState, MAX_TRACK_POINTS, SESSION_NAME


def telemetry(session="session-1", ins=True, gnss=True):
    measurement = {
        "ins_lat": 40.0001 if ins else None,
        "ins_lon": -74.0001 if ins else None,
        "ins_pos_u": 0.5 if ins else None,
        "gnss_lat": 40.0002 if gnss else None,
        "gnss_lon": -74.0002 if gnss else None,
        "gnss_pos_u_n": 1.2,
        "gnss_pos_u_e": 1.4,
    }
    return {
        "version": 1,
        "session_id": session,
        "timestamp": "2026-01-01T00:00:00Z",
        "measurement": measurement,
    }


class DashboardTests(unittest.TestCase):
    def test_pin_file_uses_salted_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            pin = create_auth_file(path, "123456")
            record = json.loads(path.read_text())
            self.assertEqual(pin, "123456")
            self.assertNotIn("123456", path.read_text())
            self.assertTrue(verify_pin("123456", record))
            self.assertFalse(verify_pin("123455", record))

    def test_local_coordinates_are_meter_scaled(self):
        east, north = local_xy(40.001, -73.999, 40.0, -74.0)
        self.assertAlmostEqual(north, 111.2, delta=0.3)
        self.assertAlmostEqual(east, 85.2, delta=0.3)

    def test_ins_origin_and_gnss_fallback_create_segments(self):
        state = DashboardState(Path("."))
        state.ingest(telemetry(ins=True))
        self.assertEqual(state.trail[-1]["source"], "ins")
        state.ingest(telemetry(ins=False, gnss=True))
        self.assertEqual(state.trail[-1]["source"], "gnss")
        self.assertEqual(state.trail[-1]["segment"], 1)

    def test_no_gnss_plot_before_ins_origin(self):
        state = DashboardState(Path("."))
        state.ingest(telemetry(ins=False, gnss=True))
        self.assertFalse(state.trail)
        self.assertFalse(state.latest["origin_ready"])

    def test_new_session_clears_origin_and_trail(self):
        state = DashboardState(Path("."))
        state.ingest(telemetry("one"))
        self.assertTrue(state.trail)
        state.ingest(telemetry("two", ins=False, gnss=True))
        self.assertFalse(state.trail)
        self.assertIsNone(state.origin)

    def test_trail_is_bounded(self):
        state = DashboardState(Path("."))
        state.ingest(telemetry())
        first = state.latest
        for index in range(MAX_TRACK_POINTS + 2):
            first["sequence"] = index
            state.ingest(first)
        self.assertEqual(len(state.trail), MAX_TRACK_POINTS)

    def test_five_viewers_receive_latest_telemetry_without_backlog(self):
        state = DashboardState(Path("."))
        queues = {asyncio.Queue(maxsize=1) for _ in range(5)}
        state.clients.update(queues)
        state.ingest(telemetry())
        second = telemetry()
        second["sequence"] = 2
        state.ingest(second)
        self.assertEqual(len(state.clients), 5)
        for queue in queues:
            self.assertEqual(queue.qsize(), 1)
            self.assertEqual(queue.get_nowait()["sequence"], 2)

    def test_download_filename_validation_rejects_traversal(self):
        self.assertIsNotNone(
            SESSION_NAME.fullmatch("rfr_vn300_20260729_120000_123456.csv")
        )
        self.assertIsNone(SESSION_NAME.fullmatch("../rfr_vn300_1.csv"))
        self.assertIsNone(SESSION_NAME.fullmatch("rfr_vn300_1.csv/../../secret"))


if __name__ == "__main__":
    unittest.main()
