#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version> [arch]" >&2
  exit 2
fi
ARCH="${2:-}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"

BIN="${DIST_DIR}/dataplicity"
if [[ ! -f "${BIN}" ]]; then
  echo "Expected binary at ${BIN}. Build it first (pyinstaller)." >&2
  exit 1
fi

PKG_ROOT="$(mktemp -d)"
trap 'rm -rf "${PKG_ROOT}"' EXIT

mkdir -p "${PKG_ROOT}/usr/local/bin"
cp "${BIN}" "${PKG_ROOT}/usr/local/bin/dataplicity"
chmod 755 "${PKG_ROOT}/usr/local/bin/dataplicity"

SUFFIX=""
if [[ -n "${ARCH}" ]]; then
  SUFFIX="-${ARCH}"
fi
OUT_PKG="${DIST_DIR}/dataplicity-cli-${VERSION}-macos${SUFFIX}.pkg"

pkgbuild \
  --root "${PKG_ROOT}" \
  --identifier "com.dataplicity.cli" \
  --version "${VERSION}" \
  --install-location "/" \
  "${OUT_PKG}"

echo "Wrote ${OUT_PKG}"
