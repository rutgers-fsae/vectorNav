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
override the defaults. Use `--no-start` to install and enable the services
without starting them immediately:

```bash
sudo ./deploy/install-rpi.sh --port /dev/ttyUSB0 --rate 40 --no-start
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

Build the core-only extension with one compiler process. Build isolation
installs pybind11 temporarily; it is not retained as a runtime dependency:

```bash
sudo -u vectornav python3 -m venv /opt/vectornav/venv
sudo -u vectornav /opt/vectornav/venv/bin/python -m pip install --upgrade pip
sudo -u vectornav env CXXFLAGS=-O2 MAX_JOBS=1 \
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
