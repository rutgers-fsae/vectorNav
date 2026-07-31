#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname -- "${SCRIPT_DIR}")"
OUTPUT_DIR="${1:-${SOURCE_DIR}/dist}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

command -v docker >/dev/null 2>&1 || {
    echo "error: Docker is required" >&2
    exit 1
}

mkdir -p "${OUTPUT_DIR}"

docker buildx build \
    --platform linux/arm64 \
    --build-arg "PYTHON_VERSION=${PYTHON_VERSION}" \
    --file "${SCRIPT_DIR}/Dockerfile.wheel" \
    --output "type=local,dest=${OUTPUT_DIR}" \
    "${SOURCE_DIR}"

echo
echo "ARM64 CPython ${PYTHON_VERSION} wheel written to ${OUTPUT_DIR}:"
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name 'vectornav-*.whl' -print
