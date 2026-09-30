"""Log Nano CSV telemetry from a Raspberry Pi GPIO UART (stdlib only)."""
import argparse
import csv
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import select
import signal
import termios

HEADER = ["received_at_utc", "time_ms", "coolant_temp_c", "fan_duty_percent", "pump_duty_percent"]


def parse_sample(line):
    try:
        fields = line.decode("ascii").strip().split(",")
        if len(fields) != 4:
            return None
        uptime, temperature, fan, pump = int(fields[0]), float(fields[1]), int(fields[2]), int(fields[3])
        if not (0 <= uptime <= 0xFFFFFFFF and math.isfinite(temperature)
                and 0 <= fan <= 100 and 0 <= pump <= 100):
            return None
        return uptime, temperature, fan, pump
    except (UnicodeError, ValueError):
        return None


def record(fd, output, stopped):
    writer = csv.writer(output)
    writer.writerow(HEADER)
    output.flush()
    pending = bytearray()
    discard = False
    while not stopped():
        if not select.select([fd], [], [], 1)[0]:
            continue
        chunk = os.read(fd, 256)
        if not chunk:
            raise OSError("UART disconnected")
        for byte in chunk:
            if byte == 10:
                sample = None if discard else parse_sample(bytes(pending))
                pending.clear()
                discard = False
                if sample is not None:
                    writer.writerow([datetime.now(timezone.utc).isoformat(), *sample])
                    output.flush()
                    os.fsync(output.fileno())
            elif not discard:
                pending.append(byte)
                if len(pending) > 128:
                    pending.clear()
                    discard = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--output-dir", type=Path, default=Path("/var/lib/vectornav/logs/coolant"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        settings = termios.tcgetattr(fd)
        settings[0] = settings[1] = settings[3] = 0
        settings[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        settings[4] = settings[5] = termios.B115200
        settings[6][termios.VMIN] = 1
        settings[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        filename = args.output_dir / f"coolant_{stamp}.csv"
        with filename.open("x", newline="", encoding="utf-8") as output:
            print(f"Recording {args.port} to {filename}", flush=True)
            record(fd, output, lambda: stopped)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
