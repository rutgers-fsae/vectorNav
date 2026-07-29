import csv
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest

import rfr_vn300_logger as logger


def namespace(**values):
    return SimpleNamespace(**values)


class FakeBinaryOutput:
    def __init__(self):
        self.asyncMode = namespace(serial1=0)
        self.rateDivisor = 0
        self.common = namespace(
            timeStartup=0,
            timeGps=0,
            ypr=0,
            accel=0,
            angularRate=0,
            imu=0,
            magPres=0,
            deltas=0,
        )
        self.gnss = namespace(
            gnss1Fix=0,
            gnss1NumSats=0,
            gnss1PosLla=0,
            gnss1VelNed=0,
            gnss1PosUncertainty=0,
        )
        self.ins = namespace(posLla=0, velNed=0, posU=0, velU=0)


class FakeBinaryOutput2(FakeBinaryOutput):
    pass


class FakeBinaryOutput3(FakeBinaryOutput):
    pass


class FakeSensor:
    def __init__(self):
        self.registers = []
        self.measurements = []
        self.async_error_checks = 0

    def writeRegister(self, register):
        self.registers.append(register)

    def getNextMeasurement(self):
        return self.measurements.pop(0)

    def throwIfAsyncError(self):
        self.async_error_checks += 1


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
            deltaTheta=namespace(
                deltaTheta=(10.0, 11.0, 12.0), deltaTime=0.1
            ),
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
    def test_parse_args_uses_pi_defaults(self):
        args = logger.parse_args([])
        self.assertEqual(args.port, "/dev/ttyUSB0")
        self.assertEqual(args.baud, 460800)
        self.assertEqual(args.rate, 40)
        self.assertEqual(args.output_dir, Path.cwd())

    def test_rate_divisor_validation(self):
        with self.assertRaises(SystemExit):
            logger.parse_args(["--rate", "0"])
        with self.assertRaises(SystemExit):
            logger.parse_args(["--rate", "65536"])

    def test_configures_one_complete_output_and_disables_others(self):
        sensor = FakeSensor()
        registers = namespace(
            System=namespace(
                BinaryOutput1=FakeBinaryOutput,
                BinaryOutput2=FakeBinaryOutput2,
                BinaryOutput3=FakeBinaryOutput3,
            )
        )

        output = logger.configure_binary_output(sensor, registers, 40)

        self.assertEqual(len(sensor.registers), 3)
        self.assertIs(sensor.registers[0], output)
        self.assertEqual(output.asyncMode.serial1, 1)
        self.assertEqual(output.rateDivisor, 40)
        self.assertEqual(output.common.imu, 0)
        self.assertEqual(output.gnss.gnss1PosUncertainty, 1)
        self.assertEqual(output.ins.posU, 1)
        self.assertEqual(output.ins.velU, 1)
        self.assertEqual(sensor.registers[1].asyncMode.serial1, 0)
        self.assertEqual(sensor.registers[2].asyncMode.serial1, 0)

    def test_complete_measurement_includes_uncertainties(self):
        row = logger.measurement_to_row(
            complete_measurement(), timestamp="2026-01-01T00:00:00+00:00"
        )

        self.assertEqual(row["gnss_pos_u_n"], 0.1)
        self.assertEqual(row["gnss_pos_u_e"], 0.2)
        self.assertEqual(row["gnss_pos_u_d"], 0.3)
        self.assertEqual(row["ins_pos_u"], 0.4)
        self.assertEqual(row["ins_vel_u"], 0.5)
        self.assertEqual(row["gnss_speed"], 5.0)
        self.assertEqual(set(row), set(logger.COLS))

    def test_missing_optional_values_produce_a_sparse_valid_row(self):
        measurement = complete_measurement()
        measurement.gnss.gnss1PosUncertainty = None
        measurement.ins.posU = None
        measurement.ins.velU = None

        row = logger.measurement_to_row(measurement, timestamp="timestamp")

        self.assertNotIn("gnss_pos_u_n", row)
        self.assertNotIn("ins_pos_u", row)
        self.assertNotIn("ins_vel_u", row)
        self.assertTrue(set(row).issubset(logger.COLS))

    def test_logging_ignores_unrelated_packets_and_writes_one_complete_row(self):
        sensor = FakeSensor()
        sensor.measurements = [
            complete_measurement(matches=False),
            complete_measurement(matches=True),
        ]
        csv_file = StringIO()
        sync_calls = []
        clock_values = iter((0.0, 0.1))

        rows = logger.log_measurements(
            sensor,
            object(),
            csv_file,
            lambda: not sensor.measurements,
            monotonic=lambda: next(clock_values),
            sync=lambda file_object: sync_calls.append(file_object),
        )

        self.assertEqual(rows, 1)
        self.assertEqual(sensor.async_error_checks, 2)
        self.assertEqual(sync_calls, [csv_file])
        records = list(csv.DictReader(StringIO(csv_file.getvalue())))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["gnss_pos_u_d"], "0.3")
        self.assertEqual(records[0]["ins_vel_u"], "0.5")


if __name__ == "__main__":
    unittest.main()
