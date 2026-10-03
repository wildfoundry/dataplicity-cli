#!/usr/bin/env bash
set -euo pipefail

# Boot a Debian cloud VM, install the Linux CLI packages, and prove port
# forwarding through the packaged binary. Intended for maintainer/local smoke.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${WORKDIR:-${ROOT_DIR}/tmp-release/linux-vm}"
IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2}"
SSH_PORT="${SSH_PORT:-2222}"
PYTHON="${PYTHON:-python3}"
VERSION="${VERSION:-$("${PYTHON}" "${ROOT_DIR}/build/get_version.py")}"
DEB_ARCH="${DEB_ARCH:-amd64}"
RPM_ARCH="${RPM_ARCH:-x86_64}"
PACKAGE_NAME="dataplicity-cli"

DEB="${ROOT_DIR}/dist/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb"
RPM="${ROOT_DIR}/dist/${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm"
TARBALL="${ROOT_DIR}/dist/${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}.tar.gz"

for artifact in "${DEB}" "${RPM}" "${TARBALL}"; do
  if [[ ! -f "${artifact}" ]]; then
    echo "Build packages first; missing ${artifact}" >&2
    exit 1
  fi
done

mkdir -p "${WORKDIR}"
IMAGE="${WORKDIR}/debian-12.qcow2"
DISK="${WORKDIR}/vm.qcow2"
SEED="${WORKDIR}/seed.iso"
KEY="${WORKDIR}/id_ed25519"
USER_DATA="${WORKDIR}/user-data"
META_DATA="${WORKDIR}/meta-data"
QEMU_LOG="${WORKDIR}/qemu.log"
GATEWAY_LOG="${WORKDIR}/gateway.log"

QEMU_PID=""
GATEWAY_PID=""
cleanup() {
  if [[ -n "${GATEWAY_PID}" ]]; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  if [[ -n "${QEMU_PID}" ]]; then
    kill "${QEMU_PID}" 2>/dev/null || true
    wait "${QEMU_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ ! -f "${IMAGE}" ]]; then
  curl -fsSL -o "${IMAGE}.partial" "${IMAGE_URL}"
  mv "${IMAGE}.partial" "${IMAGE}"
fi

rm -f "${DISK}"
qemu-img create -f qcow2 -F qcow2 -b "${IMAGE}" "${DISK}" 8G >/dev/null

if [[ ! -f "${KEY}" ]]; then
  ssh-keygen -t ed25519 -N "" -f "${KEY}" >/dev/null
fi
PUBKEY="$(cat "${KEY}.pub")"

cat >"${USER_DATA}" <<EOF
#cloud-config
users:
  - name: debian
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
    ssh_authorized_keys:
      - ${PUBKEY}
packages:
  - curl
  - rpm
  - ca-certificates
ssh_pwauth: false
runcmd:
  - [ sh, -c, "systemctl enable --now ssh || systemctl enable --now sshd || true" ]
EOF
cat >"${META_DATA}" <<EOF
instance-id: dataplicity-cli-linux-smoke
local-hostname: dp-cli-linux
EOF

if command -v cloud-localds >/dev/null 2>&1; then
  cloud-localds "${SEED}" "${USER_DATA}" "${META_DATA}"
else
  CLOUD_DIR="${WORKDIR}/cidata"
  rm -rf "${CLOUD_DIR}"
  mkdir -p "${CLOUD_DIR}"
  cp "${USER_DATA}" "${CLOUD_DIR}/user-data"
  cp "${META_DATA}" "${CLOUD_DIR}/meta-data"
  genisoimage -output "${SEED}" -volid cidata -joliet -rock "${CLOUD_DIR}/user-data" "${CLOUD_DIR}/meta-data" >/dev/null
fi

KVM_ARGS=()
if [[ -r /dev/kvm ]]; then
  KVM_ARGS=(-enable-kvm -cpu host)
else
  KVM_ARGS=(-cpu qemu64)
fi

qemu-system-x86_64 \
  "${KVM_ARGS[@]}" \
  -m 1024 \
  -smp 2 \
  -drive "file=${DISK},if=virtio,format=qcow2" \
  -drive "file=${SEED},if=virtio,format=raw,readonly=on" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
  -device virtio-net-pci,netdev=net0 \
  -display none \
  -serial file:"${QEMU_LOG}" \
  &
QEMU_PID=$!

ssh_cmd() {
  ssh -i "${KEY}" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 \
    -o LogLevel=ERROR \
    -p "${SSH_PORT}" \
    debian@127.0.0.1 \
    "$@"
}

echo "Waiting for VM SSH on localhost:${SSH_PORT}..."
for _ in $(seq 1 90); do
  if ssh_cmd 'echo up' >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${QEMU_PID}" 2>/dev/null; then
    echo "QEMU exited before SSH became ready" >&2
    cat "${QEMU_LOG}" >&2 || true
    exit 1
  fi
  sleep 2
done
ssh_cmd 'echo up' >/dev/null

free_port() {
  "${PYTHON}" - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.bind(("0.0.0.0", 0))
    print(probe.getsockname()[1])
PY
}

API_PORT="$(free_port)"
M2M_PORT="$(free_port)"
DEVICE_PORT="$(free_port)"
LOCAL_PORT=18080
CONFIG_HOST="${WORKDIR}/cli.json"

"${PYTHON}" "${ROOT_DIR}/scripts/port_forward_mock_gateway.py" \
  --host 0.0.0.0 \
  --api-port "${API_PORT}" \
  --m2m-port "${M2M_PORT}" \
  --device-port "${DEVICE_PORT}" \
  --public-host 10.0.2.2 \
  --write-config "${CONFIG_HOST}" \
  >"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!
sleep 1
if ! kill -0 "${GATEWAY_PID}" 2>/dev/null; then
  echo "Mock gateway failed to start" >&2
  cat "${GATEWAY_LOG}" >&2 || true
  exit 1
fi

scp -i "${KEY}" \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR \
  -P "${SSH_PORT}" \
  "${DEB}" "${RPM}" "${TARBALL}" "${CONFIG_HOST}" \
  debian@127.0.0.1:~/

ssh_cmd "sudo apt-get update -qq && sudo apt-get install -y ./${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb"
ssh_cmd "dataplicity --version"
ssh_cmd "dataplicity --help >/dev/null"

ssh_cmd "nohup dataplicity --config ./cli.json --json devices port-forward testdevhash --remote-port 80 --local-port ${LOCAL_PORT} >port-forward.log 2>&1 </dev/null & echo \$! > port-forward.pid"
BODY=""
for _ in $(seq 1 40); do
  if BODY="$(ssh_cmd "curl -fsS --max-time 1 http://127.0.0.1:${LOCAL_PORT}/" 2>/dev/null)"; then
    break
  fi
  sleep 0.5
done
if [[ "${BODY}" != "dataplicity-cli linux port-forward ok" && "${BODY}" != "dataplicity-cli linux port-forward ok"$'\n' ]]; then
  echo "Debian VM port-forward failed: ${BODY!r}" >&2
  ssh_cmd "cat port-forward.log" >&2 || true
  cat "${GATEWAY_LOG}" >&2 || true
  exit 1
fi
ssh_cmd "kill \$(cat port-forward.pid) 2>/dev/null || true"
echo "deb install + port-forward passed in Debian VM"

ssh_cmd "sudo apt-get remove -y ${PACKAGE_NAME}"
ssh_cmd "sudo rpm --install --nodeps ./${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm"
ssh_cmd "dataplicity --version"
ssh_cmd "sudo rpm --erase ${PACKAGE_NAME}"
echo "rpm install/remove passed in Debian VM"

ssh_cmd "tar -xzf ${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}.tar.gz"
ssh_cmd "./${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}/dataplicity --version"
echo "tarball binary passed in Debian VM"
echo "Linux VM smoke passed"
