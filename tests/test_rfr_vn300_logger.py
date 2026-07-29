import csv
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import rfr_vn300_logger as logger


def namespace(**values):
    return SimpleNamespace(**values)


class FakeBinaryOutput:
    def __init__(self):
        self.asyncMode = namespace(serial1=0)
        self.rateDivisor = 0
        self.common = namespace(
            timeStartup=0, timeGps=0, ypr=0, accel=0, angularRate=0,
            imu=0, magPres=0, deltas=0,
        )
        self.gnss = namespace(
            gnss1Fix=0, gnss1NumSats=0, gnss1PosLla=0,
            gnss1VelNed=0, gnss1PosUncertainty=0,
        )
        self.ins = namespace(posLla=0, velNed=0, posU=0, velU=0)


class FakeSensor:
    def __init__(self):
        self.registers = []
        self.disconnected = False

    def writeRegister(self, register):
        self.registers.append(register)

    def disconnect(self):
        self.disconnected = True


class FakeIpc:
    def __init__(self):
        self.published = []

    def publish(self, payload):
        self.published.append(payload)


def complete_measurement(matches=True):
    measurement = namespace(
        time=namespace(
            timeStartup=namespace(nanoseconds=lambda: 111),
            timeGps=namespace(nanoseconds=lambda: 222),
        ),
        attitude=namespace(ypr=namespace(yaw=1.0, pitch=2.0, roll=3.0)),
        imu=namespace(
            accel=(4.0, 5.0, 6.0),
            angularRate=(7.0, 8.0, 9.0),
            deltaTheta=namespace(deltaTheta=(10.0, 11.0, 12.0), deltaTime=0.1),
            deltaVel=(13.0, 14.0, 15.0),
            mag=(16.0, 17.0, 18.0),
            temperature=19.0,
            pressure=100_000.0,
        ),
        gnss=namespace(
            gnss1Fix=3,
            gnss1NumSats=12,
            gnss1PosLla=namespace(lat=40.0, lon=-74.0, alt=20.0),
            gnss1VelNed=(3.0, 4.0, 0.0),
            gnss1PosUncertainty=(0.1, 0.2, 0.3),
        ),
        ins=namespace(
            posLla=namespace(lat=41.0, lon=-75.0, alt=21.0),
            velNed=(1.0, 2.0, 3.0),
            posU=0.4,
            velU=0.5,
        ),
    )
    measurement.matchesMessage = lambda _output: matches
    return measurement


class LoggerTests(unittest.TestCase):
    def test_parse_args_uses_pi_and_ipc_defaults(self):
        args = logger.parse_args([])
        self.assertEqual(args.port, "/dev/ttyUSB0")
        self.assertEqual(args.baud, 460800)
        self.assertEqual(args.rate, 40)
        self.assertEqual(args.control_socket, logger.DEFAULT_CONTROL_SOCKET)

    def test_configures_one_complete_output(self):
        sensor = FakeSensor()
        registers = namespace(
            System=namespace(
                BinaryOutput1=FakeBinaryOutput,
                BinaryOutput2=FakeBinaryOutput,
                BinaryOutput3=FakeBinaryOutput,
            )
        )
        output = logger.configure_binary_output(sensor, registers, 40)
        self.assertEqual(len(sensor.registers), 3)
        self.assertEqual(output.common.imu, 0)
        self.assertEqual(output.gnss.gnss1PosUncertainty, 1)
        self.assertEqual(output.ins.posU, 1)
        self.assertEqual(sensor.registers[1].asyncMode.serial1, 0)

    def test_measurement_contains_uncertainties(self):
        row = logger.measurement_to_row(complete_measurement(), "timestamp")
        self.assertEqual(row["gnss_pos_u_n"], 0.1)
        self.assertEqual(row["gnss_pos_u_e"], 0.2)
        self.assertEqual(row["gnss_pos_u_d"], 0.3)
        self.assertEqual(row["ins_pos_u"], 0.4)
        self.assertEqual(row["ins_vel_u"], 0.5)
        self.assertTrue(set(row).issubset(logger.COLS))

    def test_missing_dashboard_socket_never_blocks_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            ipc = logger.LocalIpc(
                Path(directory) / "control.sock",
                Path(directory) / "missing-dashboard.sock",
            )
            ipc.open()
            ipc.publish({"version": 1})
            ipc.close()

    def make_engine(self, directory, clock=None):
        sensor = FakeSensor()
        ipc = FakeIpc()
        clock = clock or (lambda: 100.0)
        engine = logger.LoggerEngine(
            sensor,
            object(),
            Path(directory),
            Path(directory) / "config.json",
            ipc,
            lambda: (FakeSensor(), object(), "VN-300"),
            monotonic=clock,
        )
        engine.model = "VN-300"
        return engine, ipc

    def test_session_lifecycle_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, _ipc = self.make_engine(directory)
            engine.start_session()
            first_file = engine.filename
            result = engine.handle_command(
                {
                    "request_id": "1",
                    "command": "marker",
                    "payload": {"type": "Cone", "note": "turn 4"},
                }
            )
            self.assertTrue(result["success"])
            engine.consume_measurement(complete_measurement())
            engine.handle_command({"command": "new_session"})
            self.assertNotEqual(engine.filename, first_file)
            engine.stop_session()

            with first_file.open(newline="") as csv_file:
                records = list(csv.DictReader(csv_file))
            self.assertEqual(records[0]["event_type"], "Cone")
            self.assertEqual(records[0]["event_note"], "turn 4")

    def test_stop_keeps_telemetry_and_start_creates_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, ipc = self.make_engine(directory)
            engine.start_session()
            engine.handle_command({"command": "stop"})
            self.assertFalse(engine.recording)
            engine.consume_measurement(complete_measurement())
            self.assertTrue(ipc.published)
            engine.handle_command({"command": "start"})
            self.assertTrue(engine.recording)
            engine.stop_session()

    def test_set_start_finish_persists_and_resets_laps(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, _ipc = self.make_engine(directory)
            engine.latest_row = {"ins_lat": 40.2, "ins_lon": -74.1}
            engine.lap_count = 4
            response = engine.handle_command({"command": "set_start_finish"})
            self.assertTrue(response["success"])
            self.assertEqual(engine.lap_count, 0)
            loaded = logger.load_track_config(Path(directory) / "config.json")
            self.assertEqual(loaded, {"start_lat": 40.2, "start_lon": -74.1})

    def test_reconnect_rotates_active_session(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, _ipc = self.make_engine(directory)
            engine.start_session()
            previous = engine.filename
            response = engine.handle_command({"command": "reconnect"})
            self.assertTrue(response["success"])
            self.assertTrue(engine.recording)
            self.assertNotEqual(engine.filename, previous)
            self.assertEqual(engine.model, "VN-300")
            engine.stop_session()


if __name__ == "__main__":
    unittest.main()
