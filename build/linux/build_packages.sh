#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "Usage: $0 <version> [deb_arch] [rpm_arch]" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/build/linux/metadata.env"

DIST_DIR="${ROOT_DIR}/dist"
PYI_DIST_DIR="${ROOT_DIR}/pyinstaller-dist"
BIN="${PYI_DIST_DIR}/${COMMAND_NAME}"

if [[ ! -f "${BIN}" ]]; then
  echo "Expected binary at ${BIN}. Build it first (pyinstaller)." >&2
  exit 1
fi

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build Debian and RPM packages." >&2
  exit 1
fi

HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
  x86_64|amd64)
    DEFAULT_DEB_ARCH="amd64"
    DEFAULT_RPM_ARCH="x86_64"
    ;;
  aarch64|arm64)
    DEFAULT_DEB_ARCH="arm64"
    DEFAULT_RPM_ARCH="aarch64"
    ;;
  *)
    echo "Unsupported host architecture: ${HOST_ARCH}" >&2
    exit 1
    ;;
esac

DEB_ARCH="${2:-${DEFAULT_DEB_ARCH}}"
RPM_ARCH="${3:-${DEFAULT_RPM_ARCH}}"

mkdir -p "${DIST_DIR}"

STAGE="$(mktemp -d)"
ARCHIVE_ROOT="$(mktemp -d)"
trap 'rm -rf "${STAGE}" "${ARCHIVE_ROOT}"' EXIT

mkdir -p \
  "${STAGE}${INSTALL_PATH}" \
  "${STAGE}/usr/share/doc/${PACKAGE_NAME}"
install -m 0755 "${BIN}" "${STAGE}${INSTALL_PATH}/${COMMAND_NAME}"
install -m 0644 "${ROOT_DIR}/LICENSE" "${STAGE}/usr/share/doc/${PACKAGE_NAME}/"
install -m 0644 "${ROOT_DIR}/README.md" "${STAGE}/usr/share/doc/${PACKAGE_NAME}/"
install -m 0644 "${ROOT_DIR}/SECURITY.md" "${STAGE}/usr/share/doc/${PACKAGE_NAME}/"

ARCHIVE_NAME="${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}"
mkdir -p "${ARCHIVE_ROOT}/${ARCHIVE_NAME}"
install -m 0755 "${BIN}" "${ARCHIVE_ROOT}/${ARCHIVE_NAME}/${COMMAND_NAME}"
install -m 0644 "${ROOT_DIR}/LICENSE" "${ARCHIVE_ROOT}/${ARCHIVE_NAME}/"
install -m 0644 "${ROOT_DIR}/README.md" "${ARCHIVE_ROOT}/${ARCHIVE_NAME}/"
tar -C "${ARCHIVE_ROOT}" -czf "${DIST_DIR}/${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}"

fpm \
  --input-type dir \
  --output-type deb \
  --name "${PACKAGE_NAME}" \
  --version "${VERSION}" \
  --architecture "${DEB_ARCH}" \
  --license "${LICENSE}" \
  --url "${HOMEPAGE}" \
  --description "${DESCRIPTION}" \
  --maintainer "${MAINTAINER}" \
  --deb-compression xz \
  --chdir "${STAGE}" \
  --package "${DIST_DIR}/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb" \
  .

fpm \
  --input-type dir \
  --output-type rpm \
  --name "${PACKAGE_NAME}" \
  --version "${VERSION}" \
  --iteration 1 \
  --architecture "${RPM_ARCH}" \
  --license "${LICENSE}" \
  --url "${HOMEPAGE}" \
  --description "${DESCRIPTION}" \
  --maintainer "${MAINTAINER}" \
  --chdir "${STAGE}" \
  --package "${DIST_DIR}/${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm" \
  .

echo "Wrote:"
echo "  ${DIST_DIR}/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb"
echo "  ${DIST_DIR}/${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm"
echo "  ${DIST_DIR}/${ARCHIVE_NAME}.tar.gz"
