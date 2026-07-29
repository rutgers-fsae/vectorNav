#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="/opt/vectornav"
VENV_DIR="${INSTALL_DIR}/venv"
LOG_DIR="/var/lib/vectornav/logs"
SERVICE_USER="vectornav"
SERVICE_NAME="vectornav-logger.service"
DASHBOARD_SERVICE_NAME="vectornav-dashboard.service"
CONFIG_FILE="/etc/default/vectornav-logger"
AUTH_FILE="/var/lib/vectornav/dashboard-auth.json"
PORT="/dev/ttyUSB0"
BAUD="460800"
RATE="40"
DASHBOARD_PORT="8080"
START_SERVICE=1

usage() {
    cat <<'EOF'
Install the RFR VN-300 logger on Raspberry Pi OS 64-bit.

Usage:
  sudo ./deploy/install-rpi.sh [options]

Options:
  --port DEVICE     Serial device (default: /dev/ttyUSB0)
  --baud RATE       Serial baud rate (default: 460800)
  --rate DIVISOR    VN-300 400 Hz rate divisor (default: 40, or 10 Hz)
  --dashboard-port  Dashboard HTTP port (default: 8080)
  --no-start        Install and enable the service without starting it now
  -h, --help        Show this help
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

log() {
    echo
    echo "==> $*"
}

while (($# > 0)); do
    case "$1" in
        --port)
            (($# >= 2)) || die "--port requires a value"
            PORT="$2"
            shift 2
            ;;
        --baud)
            (($# >= 2)) || die "--baud requires a value"
            BAUD="$2"
            shift 2
            ;;
        --rate)
            (($# >= 2)) || die "--rate requires a value"
            RATE="$2"
            shift 2
            ;;
        --dashboard-port)
            (($# >= 2)) || die "--dashboard-port requires a value"
            DASHBOARD_PORT="$2"
            shift 2
            ;;
        --no-start)
            START_SERVICE=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ ${EUID} -eq 0 ]] || die "run this installer with sudo"
[[ "$(uname -s)" == "Linux" ]] || die "this installer only supports Linux"
[[ "$(getconf LONG_BIT)" == "64" ]] ||
    die "Raspberry Pi OS 64-bit is required; install a 64-bit OS and retry"
[[ "${BAUD}" =~ ^(9600|19200|38400|57600|115200|128000|230400|460800|921600)$ ]] ||
    die "unsupported baud rate: ${BAUD}"
[[ "${RATE}" =~ ^[0-9]+$ ]] || die "--rate must be an integer"
((RATE >= 1 && RATE <= 65535)) || die "--rate must be between 1 and 65535"
[[ "${DASHBOARD_PORT}" =~ ^[0-9]+$ ]] || die "--dashboard-port must be an integer"
((DASHBOARD_PORT >= 1 && DASHBOARD_PORT <= 65535)) ||
    die "--dashboard-port must be between 1 and 65535"
[[ "${PORT}" =~ ^/dev/[A-Za-z0-9._/+:-]+$ ]] ||
    die "--port must be an absolute /dev path without whitespace"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname -- "${SCRIPT_DIR}")"
[[ -f "${SOURCE_DIR}/rfr_vn300_logger.py" ]] ||
    die "could not find rfr_vn300_logger.py above ${SCRIPT_DIR}"
[[ -f "${SOURCE_DIR}/python/pyproject.toml" ]] ||
    die "could not find the VectorNav Python package"

log "Installing Raspberry Pi OS dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes build-essential curl python3-dev python3-venv
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' ||
    die "Python 3.10+ is required; use Raspberry Pi OS Bookworm or newer"

log "Installing uv"
if [[ -x /usr/local/bin/uv ]]; then
    echo "Using existing $(/usr/local/bin/uv --version)"
elif command -v uv >/dev/null 2>&1; then
    install -m 0755 "$(command -v uv)" /usr/local/bin/uv
else
    curl -LsSf https://astral.sh/uv/install.sh |
        env UV_UNMANAGED_INSTALL=/usr/local/bin sh
fi
/usr/local/bin/uv --version

for service in "${DASHBOARD_SERVICE_NAME}" "${SERVICE_NAME}"; do
    if systemctl is-active --quiet "${service}" 2>/dev/null; then
        log "Stopping the existing ${service}"
        systemctl stop "${service}"
    fi
done

log "Creating the ${SERVICE_USER} service account"
if ! getent passwd "${SERVICE_USER}" >/dev/null; then
    useradd \
        --system \
        --home-dir "${INSTALL_DIR}" \
        --shell /usr/sbin/nologin \
        "${SERVICE_USER}"
fi
usermod --append --groups dialout "${SERVICE_USER}"

log "Installing repository files in ${INSTALL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${LOG_DIR}"
if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
    cp -a "${SOURCE_DIR}/." "${INSTALL_DIR}/"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" /var/lib/vectornav

log "Creating the Python environment"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    runuser -u "${SERVICE_USER}" -- \
        /usr/local/bin/uv venv \
        --python /usr/bin/python3 \
        "${VENV_DIR}"
else
    echo "Reusing ${VENV_DIR}"
fi

log "Building and installing the core VectorNav extension"
runuser -u "${SERVICE_USER}" -- env \
    CXXFLAGS=-O2 \
    MAX_JOBS=1 \
    /usr/local/bin/uv pip install \
    --reinstall \
    --python "${VENV_DIR}/bin/python" \
    "${INSTALL_DIR}/python"

log "Verifying the VectorNav extension"
runuser -u "${SERVICE_USER}" -- \
    "${VENV_DIR}/bin/python" -c \
    "from vectornav import Sensor, Registers; print('VectorNav import successful')"

log "Installing the dashboard"
runuser -u "${SERVICE_USER}" -- \
    /usr/local/bin/uv pip install \
    --reinstall \
    --python "${VENV_DIR}/bin/python" \
    "${INSTALL_DIR}/dashboard"

GENERATED_PIN=""
if [[ ! -f "${AUTH_FILE}" ]]; then
    log "Generating the dashboard operator PIN"
    PIN_OUTPUT="$(
        runuser -u "${SERVICE_USER}" -- \
            "${VENV_DIR}/bin/python" -m vectornav_dashboard.auth \
            --create "${AUTH_FILE}"
    )"
    GENERATED_PIN="${PIN_OUTPUT#PIN=}"
fi
chown "${SERVICE_USER}:${SERVICE_USER}" "${AUTH_FILE}"
chmod 0640 "${AUTH_FILE}"

log "Installing the systemd services"
install -m 0644 \
    "${INSTALL_DIR}/deploy/vectornav-logger.service" \
    "/etc/systemd/system/${SERVICE_NAME}"
install -m 0644 \
    "${INSTALL_DIR}/deploy/vectornav-dashboard.service" \
    "/etc/systemd/system/${DASHBOARD_SERVICE_NAME}"
{
    echo "# Generated by ${INSTALL_DIR}/deploy/install-rpi.sh"
    printf 'VECTORNAV_PORT=%s\n' "${PORT}"
    printf 'VECTORNAV_BAUD=%s\n' "${BAUD}"
    printf 'VECTORNAV_RATE=%s\n' "${RATE}"
    printf 'VECTORNAV_OUTPUT_DIR=%s\n' "${LOG_DIR}"
    printf 'VECTORNAV_DASHBOARD_PORT=%s\n' "${DASHBOARD_PORT}"
} >"${CONFIG_FILE}"
chmod 0644 "${CONFIG_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl enable "${DASHBOARD_SERVICE_NAME}"

if ((START_SERVICE == 0)); then
    log "Installation complete; services enabled but not started"
    echo "Start them with:"
    echo "  sudo systemctl start ${SERVICE_NAME} ${DASHBOARD_SERVICE_NAME}"
elif [[ ! -e "${PORT}" ]]; then
    log "Installation complete; ${PORT} is not currently present"
    systemctl restart "${DASHBOARD_SERVICE_NAME}"
    echo "The dashboard is running; the logger is enabled but was not started."
    echo "Connect the sensor, verify its device path, then run:"
    echo "  sudo systemctl start ${SERVICE_NAME}"
else
    log "Starting VectorNav services"
    systemctl restart "${SERVICE_NAME}" "${DASHBOARD_SERVICE_NAME}"
    systemctl --no-pager --full status "${SERVICE_NAME}" || {
        echo
        echo "The service did not start successfully. Inspect its log with:"
        echo "  journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
        exit 1
    }
    systemctl --no-pager --full status "${DASHBOARD_SERVICE_NAME}" || {
        echo "Inspect dashboard logs with:"
        echo "  journalctl -u ${DASHBOARD_SERVICE_NAME} -n 100 --no-pager"
        exit 1
    }
fi

echo
echo "Configuration: ${CONFIG_FILE}"
echo "CSV directory: ${LOG_DIR}"
echo "Dashboard:     http://$(hostname -I | awk '{print $1}'):${DASHBOARD_PORT}"
if [[ -n "${GENERATED_PIN}" ]]; then
    echo "Operator PIN:  ${GENERATED_PIN}"
    echo "Save this PIN now; only its salted hash is stored."
else
    echo "Operator PIN:  preserved from the existing installation"
fi
echo "Logger logs:   journalctl -u ${SERVICE_NAME} -f"
echo "Dashboard logs: journalctl -u ${DASHBOARD_SERVICE_NAME} -f"
