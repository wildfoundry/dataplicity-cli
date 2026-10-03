#!/usr/bin/env bash
set -euo pipefail

# Exercise packaged-CLI port forwarding against the local mock gateway.
# Usage: linux_port_forward_smoke.sh /path/to/dataplicity [public-host]

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARY="${1:?dataplicity binary required}"
PUBLIC_HOST="${2:-127.0.0.1}"
PYTHON="${PYTHON:-python3}"

if [[ ! -x "${BINARY}" && ! -f "${BINARY}" ]]; then
  echo "CLI binary not found: ${BINARY}" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'cleanup' EXIT

GATEWAY_PID=""
CLI_PID=""

cleanup() {
  if [[ -n "${CLI_PID}" ]]; then
    kill "${CLI_PID}" 2>/dev/null || true
    wait "${CLI_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]]; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  rm -rf "${WORKDIR}"
}

free_port() {
  "${PYTHON}" - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.bind(("127.0.0.1", 0))
    print(probe.getsockname()[1])
PY
}

API_PORT="$(free_port)"
M2M_PORT="$(free_port)"
DEVICE_PORT="$(free_port)"
LOCAL_PORT="$(free_port)"
CONFIG="${WORKDIR}/cli.json"
LOG="${WORKDIR}/gateway.log"

"${PYTHON}" "${ROOT_DIR}/scripts/port_forward_mock_gateway.py" \
  --host 0.0.0.0 \
  --api-port "${API_PORT}" \
  --m2m-port "${M2M_PORT}" \
  --device-port "${DEVICE_PORT}" \
  --public-host "${PUBLIC_HOST}" \
  --write-config "${CONFIG}" \
  >"${LOG}" 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 50); do
  if "${PYTHON}" - <<PY
import socket, sys
sock = socket.socket()
try:
    sock.settimeout(0.2)
    sock.connect(("127.0.0.1", ${API_PORT}))
except Exception:
    sys.exit(1)
finally:
    sock.close()
PY
  then
    break
  fi
  sleep 0.1
done

if ! kill -0 "${GATEWAY_PID}" 2>/dev/null; then
  echo "Mock gateway failed to start" >&2
  cat "${LOG}" >&2 || true
  exit 1
fi

"${BINARY}" --config "${CONFIG}" --json devices port-forward testdevhash \
  --remote-port 80 --local-port "${LOCAL_PORT}" \
  >"${WORKDIR}/cli.log" 2>&1 &
CLI_PID=$!

BODY=""
for _ in $(seq 1 50); do
  if BODY="$(curl -fsS --max-time 1 "http://127.0.0.1:${LOCAL_PORT}/" 2>/dev/null)"; then
    break
  fi
  if ! kill -0 "${CLI_PID}" 2>/dev/null; then
    echo "CLI port-forward exited before accepting connections" >&2
    cat "${WORKDIR}/cli.log" >&2 || true
    cat "${LOG}" >&2 || true
    exit 1
  fi
  sleep 0.2
done

EXPECTED="dataplicity-cli linux port-forward ok"
if [[ "${BODY}" != "${EXPECTED}" && "${BODY}" != "${EXPECTED}"$'\n' ]]; then
  echo "Unexpected port-forward payload: ${BODY!r}" >&2
  cat "${WORKDIR}/cli.log" >&2 || true
  cat "${LOG}" >&2 || true
  exit 1
fi

echo "port-forward smoke passed via ${BINARY} on 127.0.0.1:${LOCAL_PORT}"
