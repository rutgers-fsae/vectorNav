# RFR VectorNav VN-300 Logger

This repository contains the VectorNav SDK and the Rutgers Formula Racing
VN-300 CSV logger. The logger is configured for one combined binary packet per
sample so each CSV row contains synchronized IMU, GNSS, INS, timing, and
uncertainty data.

## Run the logger

The defaults target a Raspberry Pi with the USB serial adapter at
`/dev/ttyUSB0`, a 460800 baud connection, and a 10 Hz output rate:

```bash
python rfr_vn300_logger.py --output-dir ./logs
```

The `--rate` value is a divisor of the VN-300's 400 Hz base rate. For example,
`--rate 40` produces 10 Hz. Run `python rfr_vn300_logger.py --help` for all
options.

## Live dashboard

The Pi-local dashboard is served at `http://<pi-address>:8080`. It provides a
live east/north position plot, logger and Pi health, completed CSV downloads,
and PIN-protected operator controls for:

- Starting, stopping, and rotating recording sessions
- Adding event markers
- Resetting laps and setting the current INS position as start/finish
- Reconnecting and reconfiguring the VN-300

The position plot uses the first fused INS position in each session as `(0,0)`.
It needs no map tiles or internet access. If INS position becomes unavailable
after the origin is established, raw GNSS is plotted as a visibly degraded,
separate trail segment.

CSV logging remains at full rate. Dashboard telemetry is best-effort at 5 Hz,
so dashboard or Wi-Fi problems cannot block recording.

## Raspberry Pi Zero 2 W installation

Raspberry Pi OS Lite 64-bit Bookworm or newer with Python 3.10+ is recommended.
Install the compiler and Python development files:

For an automated installation, clone or copy this repository onto the Pi and
run:

```bash
chmod +x deploy/install-rpi.sh
sudo ./deploy/install-rpi.sh
```

Use `--port /dev/ttyACM0`, `--baud`, `--rate`, or `--dashboard-port` to
override the defaults. The installer fingerprints the native SDK sources and
skips its slow C++ rebuild when they are unchanged. Use `--rebuild` to force a
new native build, or `--no-start` to install and enable the services without
starting them immediately:

```bash
sudo ./deploy/install-rpi.sh --port /dev/ttyUSB0 --rate 40 --no-start
```

To avoid compiling C++ on the Pi, supply a compatible wheel built as described
below:

```bash
sudo ./deploy/install-rpi.sh --vectornav-wheel ./vectornav-1.0.0-cp313-cp313-linux_aarch64.whl
```

The remaining commands document the equivalent manual installation.

```bash
sudo apt update
sudo apt install --yes build-essential curl python3-dev python3-venv
curl -LsSf https://astral.sh/uv/install.sh | \
  sudo env UV_UNMANAGED_INSTALL=/usr/local/bin sh
```

For a production installation, put the checkout in `/opt/vectornav`, create a
dedicated service account, and prepare the log directory:

```bash
sudo useradd --system --home /opt/vectornav --shell /usr/sbin/nologin vectornav
sudo usermod --append --groups dialout vectornav
sudo mkdir --parents /opt/vectornav /var/lib/vectornav/logs
sudo cp --archive . /opt/vectornav/
sudo chown --recursive vectornav:vectornav /opt/vectornav /var/lib/vectornav
```

Build the core-only extension without compiler optimization on the Pi. This
substantially reduces build time and has negligible impact on this 10 Hz
logger. Build isolation installs pybind11 temporarily; it is not retained as a
runtime dependency:

```bash
sudo -u vectornav python3 -m venv /opt/vectornav/venv
sudo -u vectornav /opt/vectornav/venv/bin/python -m pip install --upgrade pip
sudo -u vectornav env CXXFLAGS="-O0 -g0" MAX_JOBS=1 \
  /opt/vectornav/venv/bin/python -m pip install /opt/vectornav/python
sudo -u vectornav /usr/local/bin/uv pip install \
  --python /opt/vectornav/venv/bin/python /opt/vectornav/dashboard
sudo -u vectornav /opt/vectornav/venv/bin/python \
  -m vectornav_dashboard.auth \
  --create /var/lib/vectornav/dashboard-auth.json
```

For multiple Pis, prefer building a wheel once on a compatible ARM64
Raspberry Pi OS system and installing that wheel on each device:

```bash
env CXXFLAGS=-O2 MAX_JOBS=1 python -m pip wheel ./python --wheel-dir ./dist
```

Do not use `-march=native` for a wheel intended for more than one device.

### Build the ARM64 wheel with Docker

Docker Desktop on an Apple Silicon Mac can build the Linux ARM64 wheel without
compiling on the Pi. From the repository root, run:

```bash
./deploy/build-arm64-wheel.sh
```

The wheel is written to `dist/`. The image currently targets CPython 3.13 on
ARM64 Linux, matching Raspberry Pi OS installations that use Python 3.13. Copy
the wheel to the Pi, pull the same repository revision, and run:

```bash
sudo ./deploy/install-rpi.sh --vectornav-wheel ./dist/vectornav-1.0.0-cp313-cp313-linux_aarch64.whl
```

The installer validates the CPython and ARM64 tags before installation. If no
wheel is supplied, it falls back to compiling the extension locally.

To target a different Python minor version, set `PYTHON_VERSION`, for example
`PYTHON_VERSION=3.12 ./deploy/build-arm64-wheel.sh`. It must match the Python
minor version reported by `python3 --version` on the Pi.

To compile all optional VectorNav Python plugins instead of the minimal core,
set `VECTORNAV_BUILD_PLUGINS=1` while building:

```bash
VECTORNAV_BUILD_PLUGINS=1 python -m pip install ./python
```

## systemd service

Install the supplied service and its editable settings:

```bash
sudo cp /opt/vectornav/deploy/vectornav-logger.service /etc/systemd/system/
sudo cp /opt/vectornav/deploy/vectornav-dashboard.service /etc/systemd/system/
sudo cp /opt/vectornav/deploy/vectornav-logger.default /etc/default/vectornav-logger
sudo systemctl daemon-reload
sudo systemctl enable --now vectornav-logger.service vectornav-dashboard.service
```

Change `/etc/default/vectornav-logger` if the serial device or rate differs.
Then restart and inspect the service:

```bash
sudo systemctl restart vectornav-logger.service vectornav-dashboard.service
systemctl status vectornav-logger.service
journalctl --unit vectornav-dashboard.service --follow
journalctl --unit vectornav-logger.service --follow
```

The installer prints a six-digit operator PIN on first installation. Save it:
only its salted hash is retained. Re-running the installer preserves the
existing PIN.

The logger and dashboard processes handle systemd shutdown cleanly. CSV data
is flushed regularly, forced to storage every five seconds, and synced once
more before a recording closes. Files are written to
`/var/lib/vectornav/logs`.

## Nano coolant logging over GPIO UART

Flash `firmware/arduino/PWM26/PWM26.ino` onto a classic ATmega328P Nano.
It sends `time_ms,coolant_temp_c,fan_duty_percent,pump_duty_percent` CSV
at 1 Hz, 115200 baud, 8N1. The Nano controls the fan and pump independently;
there is no longer an SD logger on the Nano.

For a 5 V Nano, connect D1/TX through 1 kohm to a junction connected to
Pi GPIO15/RX (physical pin 10). Connect 2 kohm from that junction to ground,
and connect Nano GND to Pi GND (physical pin 6). This divides 5 V to about
3.3 V; never connect a 5 V TX directly to Pi GPIO. No RX connection to the
Nano is needed. Keep the boards' existing power supplies and use short wiring.

On the Pi, use `sudo raspi-config` to disable the serial login shell and
enable serial hardware, then reboot. Verify `/dev/serial0` exists. The
Zero 2 W UART shares resources with Bluetooth; configure that assignment
if needed. The VectorNav must use a separate device (normally `/dev/ttyUSB0`).

Install both loggers with:

```bash
sudo ./deploy/install-rpi.sh --pwm-port /dev/serial0
journalctl -u pwm-logger.service -f
```

Repeat `--pwm-port` on later installs to retain coolant logging; omitting it
disables the service. `--no-start` enables it without starting it.
Override `PWM_PORT` in `/etc/default/vectornav-logger` if necessary.
The reader uses only the Python standard library. It skips headers, malformed
rows, and oversized lines, and restarts after serial errors. Each service
start creates a separate CSV in `/var/lib/vectornav/logs/coolant`, with UTC
receive time and Nano uptime. The dashboard shows live coolant temperature, fan/pump duty, Nano uptime,
and sample time, with stale data marked after five seconds. PWM CSVs have
their own download archive, including complete-row snapshots of active files.
Recording controls apply to VectorNav only; coolant logging runs continuously
and independently.
A temperature of -999 is the Nano's thermistor fault indicator.

Samples sent while the Pi is booting or the logger is down are lost. Receive
timestamps allow approximate comparison with VectorNav; they are not hardware
synchronization. Each accepted row is flushed and fsynced, but abrupt power
loss still depends on the SD card's durability.
