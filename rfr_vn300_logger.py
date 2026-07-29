"""RFR VN-300 logger with non-blocking local dashboard telemetry and control."""

from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime
import importlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time
from typing import Any, Callable, TextIO


LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 460800
DEFAULT_RATE_DIVISOR = 40
DEFAULT_CONTROL_SOCKET = Path("/run/vectornav/logger-control.sock")
DEFAULT_TELEMETRY_SOCKET = Path("/run/vectornav/dashboard-telemetry.sock")
DEFAULT_CONFIG_FILE = Path("/var/lib/vectornav/config.json")
SUPPORTED_BAUD_RATES = (
    9600, 19200, 38400, 57600, 115200, 128000, 230400, 460800, 921600
)
FLUSH_EVERY_ROWS = 50
FSYNC_INTERVAL_SECONDS = 5.0
TELEMETRY_INTERVAL_SECONDS = 0.2
EARTH_RADIUS_METERS = 6_371_000.0
DEFAULT_START_LAT = 40.52653
DEFAULT_START_LON = -74.46526
THRESHOLD_METERS = 5.0
COOL_DOWN_SECONDS = 20.0
MARKER_TYPES = {
    "Run Start",
    "Cone",
    "Mechanical",
    "Setup Change",
    "Flag",
    "Other",
}

COLS = [
    "timestamp", "startup_ns", "gps_utc_ns",
    "yaw", "pitch", "roll",
    "ax", "ay", "az",
    "gx", "gy", "gz",
    "dvx", "dvy", "dvz",
    "dtx", "dty", "dtz", "dt",
    "mx", "my", "mz", "temp_c", "pressure_pa",
    "gnss_fix", "gnss_sats",
    "gnss_lat", "gnss_lon", "gnss_alt",
    "gnss_vn", "gnss_ve", "gnss_vd", "gnss_speed",
    "gnss_pos_u_n", "gnss_pos_u_e", "gnss_pos_u_d",
    "ins_lat", "ins_lon", "ins_alt",
    "ins_vn", "ins_ve", "ins_vd", "ins_pos_u", "ins_vel_u",
    "event_timestamp", "event_type", "event_note",
]


def positive_rate_divisor(value: str) -> int:
    divisor = int(value)
    if not 1 <= divisor <= 65_535:
        raise argparse.ArgumentTypeError("rate divisor must be between 1 and 65535")
    return divisor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VN-300 data logger")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument(
        "--baud", type=int, choices=SUPPORTED_BAUD_RATES, default=DEFAULT_BAUD
    )
    parser.add_argument(
        "--rate",
        type=positive_rate_divisor,
        default=DEFAULT_RATE_DIVISOR,
        metavar="DIVISOR",
        help="VN-300 400 Hz base-rate divisor (40 produces 10 Hz)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--control-socket", type=Path, default=DEFAULT_CONTROL_SOCKET)
    parser.add_argument(
        "--telemetry-socket", type=Path, default=DEFAULT_TELEMETRY_SOCKET
    )
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def distance_meters(
    lat: float, lon: float, target_lat: float, target_lon: float
) -> float:
    lat_radians = math.radians(lat)
    target_lat_radians = math.radians(target_lat)
    dlat = lat_radians - target_lat_radians
    dlon = math.radians(lon - target_lon)
    haversine = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat_radians)
        * math.cos(target_lat_radians)
        * math.sin(dlon / 2.0) ** 2
    )
    return EARTH_RADIUS_METERS * 2.0 * math.asin(math.sqrt(haversine))


def configure_binary_output(sensor: Any, registers: Any, rate: int) -> Any:
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
    measurement: Any, timestamp: str | None = None
) -> dict[str, Any]:
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
        row["dtx"], row["dty"], row["dtz"] = measurement.imu.deltaTheta.deltaTheta
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


def output_path(output_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S_%f")
    return output_dir / f"rfr_vn300_{stamp}.csv"


def load_track_config(path: Path) -> dict[str, float]:
    defaults = {"start_lat": DEFAULT_START_LAT, "start_lon": DEFAULT_START_LON}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "start_lat": float(data["start_lat"]),
            "start_lon": float(data["start_lon"]),
        }
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return defaults


def save_track_config(path: Path, data: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as config_file:
        json.dump(data, config_file, separators=(",", ":"))
        config_file.write("\n")
        sync_file(config_file)
    os.replace(temporary, path)


class LocalIpc:
    def __init__(self, control_path: Path, telemetry_path: Path):
        self.control_path = control_path
        self.telemetry_path = telemetry_path
        self.control: socket.socket | None = None
        self.telemetry = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.telemetry.setblocking(False)

    def open(self) -> None:
        try:
            self.control_path.parent.mkdir(parents=True, exist_ok=True)
            self.control_path.unlink(missing_ok=True)
            self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.control.bind(str(self.control_path))
            os.chmod(self.control_path, 0o660)
            self.control.setblocking(False)
        except OSError as error:
            if self.control is not None:
                self.control.close()
                self.control = None
            LOGGER.warning(
                "Dashboard control socket unavailable; logging will continue: %s",
                error,
            )

    def receive(self) -> tuple[dict[str, Any], str] | None:
        if self.control is None:
            return None
        try:
            raw, address = self.control.recvfrom(8192)
        except BlockingIOError:
            return None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            return request, address
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.reply(address, {"success": False, "error": str(error)})
            return None

    def reply(self, address: str, payload: dict[str, Any]) -> None:
        if self.control is None or not address:
            return
        try:
            self.control.sendto(json.dumps(payload).encode(), address)
        except OSError:
            LOGGER.debug("Control client disappeared before reply")

    def publish(self, payload: dict[str, Any]) -> None:
        try:
            self.telemetry.sendto(
                json.dumps(payload, separators=(",", ":")).encode(),
                str(self.telemetry_path),
            )
        except OSError:
            pass

    def close(self) -> None:
        if self.control is not None:
            self.control.close()
        self.telemetry.close()
        try:
            self.control_path.unlink(missing_ok=True)
        except OSError:
            pass


class LoggerEngine:
    def __init__(
        self,
        sensor: Any,
        output: Any,
        output_dir: Path,
        config_file: Path,
        ipc: LocalIpc,
        reconnect: Callable[[], tuple[Any, Any, str]],
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.sensor = sensor
        self.output = output
        self.output_dir = output_dir
        self.config_file = config_file
        self.ipc = ipc
        self.reconnect_callback = reconnect
        self.monotonic = monotonic
        self.model = ""
        self.connected = True
        self.state = "stopped"
        self.csv_file: TextIO | None = None
        self.writer: csv.DictWriter[str] | None = None
        self.filename: Path | None = None
        self.session_id: str | None = None
        self.row_count = 0
        self.last_sync_time = monotonic()
        self.last_telemetry_time = 0.0
        self.latest_row: dict[str, Any] = {}
        self.pending_markers: deque[dict[str, str]] = deque()
        self.track = load_track_config(config_file)
        self.lap_count = 0
        self.last_lap_time: float | None = None
        self.last_lap_duration: float | None = None
        self.sequence = 0

    @property
    def recording(self) -> bool:
        return self.csv_file is not None

    def reset_session_state(self) -> None:
        self.row_count = 0
        self.lap_count = 0
        self.last_lap_time = None
        self.last_lap_duration = None
        self.pending_markers.clear()

    def start_session(self) -> None:
        if self.recording:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.filename = output_path(self.output_dir)
        self.session_id = self.filename.stem
        self.csv_file = self.filename.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(
            self.csv_file, fieldnames=COLS, extrasaction="raise"
        )
        self.writer.writeheader()
        self.reset_session_state()
        self.state = "recording"
        LOGGER.info("Recording session %s", self.filename)

    def stop_session(self) -> None:
        if self.csv_file is None:
            self.state = "stopped"
            return
        sync_file(self.csv_file)
        self.csv_file.close()
        LOGGER.info("Closed %s after %d rows", self.filename, self.row_count)
        self.csv_file = None
        self.writer = None
        self.state = "stopped"

    def rotate_session(self) -> None:
        self.stop_session()
        self.start_session()

    def state_payload(self) -> dict[str, Any]:
        now = self.monotonic()
        current_lap = None
        if self.last_lap_time is not None:
            current_lap = max(0.0, now - self.last_lap_time)
        return {
            "version": 1,
            "sequence": self.sequence,
            "timestamp": datetime.now().astimezone().isoformat(),
            "connected": self.connected,
            "model": self.model,
            "state": self.state,
            "recording": self.recording,
            "session_id": self.session_id,
            "filename": self.filename.name if self.filename else None,
            "row_count": self.row_count,
            "lap_count": self.lap_count,
            "current_lap_s": current_lap,
            "last_lap_s": self.last_lap_duration,
            "start_finish": self.track,
            "measurement": self.latest_row,
        }

    def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        command = request.get("command")
        payload = request.get("payload") or {}
        try:
            if command == "start":
                self.start_session()
            elif command == "stop":
                self.stop_session()
            elif command == "new_session":
                self.rotate_session()
            elif command == "marker":
                if not self.recording:
                    raise ValueError("event markers require an active recording")
                marker_type = str(payload.get("type", "")).strip()
                note = str(payload.get("note", "")).strip()
                if marker_type not in MARKER_TYPES:
                    raise ValueError("unsupported marker type")
                if len(note) > 120:
                    raise ValueError("marker note must be 120 characters or fewer")
                self.pending_markers.append(
                    {
                        "event_timestamp": datetime.now().astimezone().isoformat(),
                        "event_type": marker_type,
                        "event_note": note,
                    }
                )
            elif command == "reset_laps":
                self.lap_count = 0
                self.last_lap_time = None
                self.last_lap_duration = None
            elif command == "set_start_finish":
                if self.latest_row.get("ins_lat") is None:
                    raise ValueError("a fused INS position is required")
                self.track = {
                    "start_lat": float(self.latest_row["ins_lat"]),
                    "start_lon": float(self.latest_row["ins_lon"]),
                }
                save_track_config(self.config_file, self.track)
                self.lap_count = 0
                self.last_lap_time = None
                self.last_lap_duration = None
            elif command == "reconnect":
                was_recording = self.recording
                self.stop_session()
                self.state = "reconnecting"
                self.connected = False
                try:
                    self.sensor.disconnect()
                except Exception:
                    LOGGER.debug("Sensor was already disconnected")
                self.sensor, self.output, self.model = self.reconnect_callback()
                self.connected = True
                self.state = "stopped"
                if was_recording:
                    self.start_session()
            else:
                raise ValueError("unknown command")
            LOGGER.info("Dashboard command succeeded: %s", command)
            return {
                "request_id": request_id,
                "success": True,
                "state": self.state_payload(),
            }
        except Exception as error:
            LOGGER.warning("Dashboard command %s failed: %s", command, error)
            return {
                "request_id": request_id,
                "success": False,
                "error": str(error),
                "state": self.state_payload(),
            }

    def process_one_command(self) -> None:
        received = self.ipc.receive()
        if received is None:
            return
        request, address = received
        self.ipc.reply(address, self.handle_command(request))

    def update_lap(self, row: dict[str, Any], now: float) -> None:
        if row.get("ins_lat") is None:
            return
        distance = distance_meters(
            row["ins_lat"],
            row["ins_lon"],
            self.track["start_lat"],
            self.track["start_lon"],
        )
        if distance >= THRESHOLD_METERS:
            return
        if (
            self.last_lap_time is not None
            and now - self.last_lap_time <= COOL_DOWN_SECONDS
        ):
            return
        if self.last_lap_time is not None:
            self.last_lap_duration = now - self.last_lap_time
            LOGGER.info(
                "Lap %d time: %.3f seconds",
                self.lap_count,
                self.last_lap_duration,
            )
        self.last_lap_time = now
        self.lap_count += 1

    def consume_measurement(self, measurement: Any) -> None:
        if not measurement.matchesMessage(self.output):
            return
        row = measurement_to_row(measurement)
        self.latest_row = row
        now = self.monotonic()
        self.update_lap(row, now)
        if self.recording and self.writer is not None and self.csv_file is not None:
            if self.pending_markers:
                row.update(self.pending_markers.popleft())
            self.writer.writerow(row)
            self.row_count += 1
            if self.row_count % FLUSH_EVERY_ROWS == 0:
                self.csv_file.flush()
            if now - self.last_sync_time >= FSYNC_INTERVAL_SECONDS:
                sync_file(self.csv_file)
                self.last_sync_time = now
        if now - self.last_telemetry_time >= TELEMETRY_INTERVAL_SECONDS:
            self.sequence += 1
            self.ipc.publish(self.state_payload())
            self.last_telemetry_time = now

    def run(self, should_stop: Callable[[], bool]) -> None:
        self.ipc.open()
        self.start_session()
        try:
            while not should_stop():
                self.process_one_command()
                measurement = self.sensor.getNextMeasurement()
                if measurement:
                    self.consume_measurement(measurement)
                self.sensor.throwIfAsyncError()
        finally:
            self.stop_session()
            self.ipc.close()
            self.sensor.disconnect()


def run(args: argparse.Namespace) -> int:
    vectornav = importlib.import_module("vectornav")
    stop_requested = False

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        LOGGER.info("Received signal %d; stopping", signum)
        stop_requested = True

    previous_handlers = {
        sig: signal.signal(sig, request_stop)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }

    def connect() -> tuple[Any, Any, str]:
        sensor = vectornav.Sensor()
        sensor.autoConnect(args.port)
        baud_rate = getattr(vectornav.Sensor.BaudRate, f"Baud{args.baud}")
        if sensor.connectedBaudRate() != baud_rate:
            sensor.changeBaudRate(baud_rate)
        if sensor.connectedBaudRate() != baud_rate:
            raise RuntimeError(f"failed to switch to {args.baud} baud")
        model_register = vectornav.Registers.System.Model()
        sensor.readRegister(model_register)
        output = configure_binary_output(sensor, vectornav.Registers, args.rate)
        LOGGER.info(
            "Connected to %s on %s at %d baud; output %.3f Hz",
            model_register.model,
            args.port,
            args.baud,
            400.0 / args.rate,
        )
        return sensor, output, model_register.model

    try:
        sensor, output, model = connect()
        engine = LoggerEngine(
            sensor,
            output,
            args.output_dir,
            args.config_file,
            LocalIpc(args.control_socket, args.telemetry_socket),
            connect,
        )
        engine.model = model
        engine.run(lambda: stop_requested)
        return 0
    except Exception:
        LOGGER.exception("VN-300 logger failed")
        return 1
    finally:
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
