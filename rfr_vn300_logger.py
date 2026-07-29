"""RFR VN-300 GPS, IMU, INS, and attitude data logger."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import importlib
import logging
import math
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, TextIO


LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 460800
DEFAULT_RATE_DIVISOR = 40
SUPPORTED_BAUD_RATES = (
    9600,
    19200,
    38400,
    57600,
    115200,
    128000,
    230400,
    460800,
    921600,
)
FLUSH_EVERY_ROWS = 50
FSYNC_INTERVAL_SECONDS = 5.0

# Lap timer configuration.
START_LAT = 40.52653
START_LON = -74.46526
THRESHOLD_METERS = 5.0
COOL_DOWN_SECONDS = 20.0
EARTH_RADIUS_METERS = 6_371_000.0
START_LAT_RADIANS = math.radians(START_LAT)

COLS = [
    "timestamp",
    "startup_ns",
    "gps_utc_ns",
    "yaw",
    "pitch",
    "roll",
    "ax",
    "ay",
    "az",
    "gx",
    "gy",
    "gz",
    "dvx",
    "dvy",
    "dvz",
    "dtx",
    "dty",
    "dtz",
    "dt",
    "mx",
    "my",
    "mz",
    "temp_c",
    "pressure_pa",
    "gnss_fix",
    "gnss_sats",
    "gnss_lat",
    "gnss_lon",
    "gnss_alt",
    "gnss_vn",
    "gnss_ve",
    "gnss_vd",
    "gnss_speed",
    "gnss_pos_u_n",
    "gnss_pos_u_e",
    "gnss_pos_u_d",
    "ins_lat",
    "ins_lon",
    "ins_alt",
    "ins_vn",
    "ins_ve",
    "ins_vd",
    "ins_pos_u",
    "ins_vel_u",
]


def positive_rate_divisor(value: str) -> int:
    divisor = int(value)
    if not 1 <= divisor <= 65_535:
        raise argparse.ArgumentTypeError("rate divisor must be between 1 and 65535")
    return divisor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VN-300 data logger")
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help=f"Serial port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--baud",
        type=int,
        choices=SUPPORTED_BAUD_RATES,
        default=DEFAULT_BAUD,
        help=f"Active serial baud rate after connecting (default: {DEFAULT_BAUD})",
    )
    parser.add_argument(
        "--rate",
        type=positive_rate_divisor,
        default=DEFAULT_RATE_DIVISOR,
        metavar="DIVISOR",
        help="VN-300 400 Hz base-rate divisor (40 produces 10 Hz)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory for timestamped CSV files (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def distance_meters(lat: float, lon: float) -> float:
    """Return the great-circle distance from a position to the start line."""
    lat_radians = math.radians(lat)
    dlat = lat_radians - START_LAT_RADIANS
    dlon = math.radians(lon - START_LON)
    haversine = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat_radians)
        * math.cos(START_LAT_RADIANS)
        * math.sin(dlon / 2.0) ** 2
    )
    return EARTH_RADIUS_METERS * 2.0 * math.asin(math.sqrt(haversine))


def configure_binary_output(sensor: Any, registers: Any, rate: int) -> Any:
    """Configure one complete output packet and disable the other two streams."""
    output = registers.System.BinaryOutput1()
    output.asyncMode.serial1 = 1
    output.rateDivisor = rate

    output.common.timeStartup = 1
    output.common.timeGps = 1
    output.common.ypr = 1
    output.common.accel = 1
    output.common.angularRate = 1
    output.common.magPres = 1
    output.common.deltas = 1

    output.gnss.gnss1Fix = 1
    output.gnss.gnss1NumSats = 1
    output.gnss.gnss1PosLla = 1
    output.gnss.gnss1VelNed = 1
    output.gnss.gnss1PosUncertainty = 1

    output.ins.posLla = 1
    output.ins.velNed = 1
    output.ins.posU = 1
    output.ins.velU = 1

    sensor.writeRegister(output)

    for output_type in (
        registers.System.BinaryOutput2,
        registers.System.BinaryOutput3,
    ):
        disabled_output = output_type()
        disabled_output.asyncMode.serial1 = 0
        sensor.writeRegister(disabled_output)

    return output


def measurement_to_row(
    measurement: Any,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Convert a combined VectorNav measurement into one CSV row."""
    row: dict[str, Any] = {
        "timestamp": timestamp or datetime.now().astimezone().isoformat()
    }

    if measurement.time.timeStartup is not None:
        row["startup_ns"] = measurement.time.timeStartup.nanoseconds()
    if measurement.time.timeGps is not None:
        row["gps_utc_ns"] = measurement.time.timeGps.nanoseconds()

    if measurement.attitude.ypr is not None:
        row["yaw"] = measurement.attitude.ypr.yaw
        row["pitch"] = measurement.attitude.ypr.pitch
        row["roll"] = measurement.attitude.ypr.roll

    if measurement.imu.accel is not None:
        row["ax"], row["ay"], row["az"] = measurement.imu.accel
    if measurement.imu.angularRate is not None:
        row["gx"], row["gy"], row["gz"] = measurement.imu.angularRate
    if measurement.imu.deltaTheta is not None:
        row["dtx"], row["dty"], row["dtz"] = (
            measurement.imu.deltaTheta.deltaTheta
        )
        row["dt"] = measurement.imu.deltaTheta.deltaTime
    if measurement.imu.deltaVel is not None:
        row["dvx"], row["dvy"], row["dvz"] = measurement.imu.deltaVel
    if measurement.imu.mag is not None:
        row["mx"], row["my"], row["mz"] = measurement.imu.mag
    if measurement.imu.temperature is not None:
        row["temp_c"] = measurement.imu.temperature
    if measurement.imu.pressure is not None:
        row["pressure_pa"] = measurement.imu.pressure

    if measurement.gnss.gnss1Fix is not None:
        row["gnss_fix"] = int(measurement.gnss.gnss1Fix)
    if measurement.gnss.gnss1NumSats is not None:
        row["gnss_sats"] = measurement.gnss.gnss1NumSats
    if measurement.gnss.gnss1PosLla is not None:
        row["gnss_lat"] = measurement.gnss.gnss1PosLla.lat
        row["gnss_lon"] = measurement.gnss.gnss1PosLla.lon
        row["gnss_alt"] = measurement.gnss.gnss1PosLla.alt
    if measurement.gnss.gnss1VelNed is not None:
        row["gnss_vn"], row["gnss_ve"], row["gnss_vd"] = (
            measurement.gnss.gnss1VelNed
        )
        row["gnss_speed"] = math.sqrt(
            row["gnss_vn"] ** 2 + row["gnss_ve"] ** 2 + row["gnss_vd"] ** 2
        )
    if measurement.gnss.gnss1PosUncertainty is not None:
        (
            row["gnss_pos_u_n"],
            row["gnss_pos_u_e"],
            row["gnss_pos_u_d"],
        ) = measurement.gnss.gnss1PosUncertainty

    if measurement.ins.posLla is not None:
        row["ins_lat"] = measurement.ins.posLla.lat
        row["ins_lon"] = measurement.ins.posLla.lon
        row["ins_alt"] = measurement.ins.posLla.alt
    if measurement.ins.velNed is not None:
        row["ins_vn"], row["ins_ve"], row["ins_vd"] = measurement.ins.velNed
    if measurement.ins.posU is not None:
        row["ins_pos_u"] = measurement.ins.posU
    if measurement.ins.velU is not None:
        row["ins_vel_u"] = measurement.ins.velU

    return row


def sync_file(csv_file: TextIO) -> None:
    csv_file.flush()
    os.fsync(csv_file.fileno())


def log_measurements(
    sensor: Any,
    output: Any,
    csv_file: TextIO,
    should_stop: Callable[[], bool],
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sync: Callable[[TextIO], None] = sync_file,
) -> int:
    writer = csv.DictWriter(csv_file, fieldnames=COLS, extrasaction="raise")
    writer.writeheader()

    row_count = 0
    lap_count = 0
    last_lap_time: float | None = None
    last_sync_time = monotonic()

    while not should_stop():
        measurement = sensor.getNextMeasurement()
        if not measurement:
            sensor.throwIfAsyncError()
            continue
        if not measurement.matchesMessage(output):
            LOGGER.debug("Ignoring an asynchronous packet from another output")
            sensor.throwIfAsyncError()
            continue

        row = measurement_to_row(measurement)
        now = monotonic()
        if row.get("ins_lat") is not None and row.get("ins_lon") is not None:
            if distance_meters(row["ins_lat"], row["ins_lon"]) < THRESHOLD_METERS:
                if (
                    last_lap_time is None
                    or now - last_lap_time > COOL_DOWN_SECONDS
                ):
                    if last_lap_time is not None:
                        LOGGER.info(
                            "Lap %d time: %.3f seconds",
                            lap_count,
                            now - last_lap_time,
                        )
                    last_lap_time = now
                    lap_count += 1

        writer.writerow(row)
        row_count += 1
        if row_count % FLUSH_EVERY_ROWS == 0:
            csv_file.flush()
        if now - last_sync_time >= FSYNC_INTERVAL_SECONDS:
            sync(csv_file)
            last_sync_time = now

        sensor.throwIfAsyncError()

    sync(csv_file)
    return row_count


def output_path(output_dir: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return output_dir / f"rfr_vn300_{timestamp}.csv"


def run(args: argparse.Namespace) -> int:
    vectornav = importlib.import_module("vectornav")
    sensor = vectornav.Sensor()
    csv_file: TextIO | None = None
    stop_requested = False
    rows_saved = 0
    filename: Path | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        LOGGER.info("Received signal %d; stopping", signum)
        stop_requested = True

    previous_handlers = {
        sig: signal.signal(sig, request_stop)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }

    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        filename = output_path(args.output_dir)
        csv_file = filename.open("w", newline="", encoding="utf-8")

        sensor.autoConnect(args.port)
        baud_rate = getattr(vectornav.Sensor.BaudRate, f"Baud{args.baud}")
        if sensor.connectedBaudRate() != baud_rate:
            sensor.changeBaudRate(baud_rate)
        if sensor.connectedBaudRate() != baud_rate:
            raise RuntimeError(f"failed to switch serial connection to {args.baud} baud")

        LOGGER.info("Connected on %s at %d baud", args.port, args.baud)
        model_register = vectornav.Registers.System.Model()
        sensor.readRegister(model_register)
        LOGGER.info("Sensor: %s", model_register.model)

        output = configure_binary_output(sensor, vectornav.Registers, args.rate)
        LOGGER.info(
            "Configured one combined binary output at %.3f Hz",
            400.0 / args.rate,
        )
        LOGGER.info("Logging to %s", filename)

        rows_saved = log_measurements(
            sensor,
            output,
            csv_file,
            lambda: stop_requested,
        )
        LOGGER.info("Stopped. %d rows saved to %s", rows_saved, filename)
        return 0
    except Exception:
        LOGGER.exception("VN-300 logger failed")
        return 1
    finally:
        if csv_file is not None:
            try:
                sync_file(csv_file)
            except OSError:
                LOGGER.exception("Failed to sync %s", filename)
            csv_file.close()
        try:
            sensor.disconnect()
        except Exception:
            LOGGER.exception("Failed to disconnect sensor")
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
