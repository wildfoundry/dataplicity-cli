#!/usr/bin/env bash
set -euo pipefail

# Smoke an already-installed or unpacked dataplicity binary, and optionally
# install/remove the generated Linux packages the same way Lens CI does.

COMMAND_NAME="${COMMAND_NAME:-dataplicity}"
PACKAGE_NAME="${PACKAGE_NAME:-dataplicity-cli}"

smoke_binary() {
  local binary="$1"
  "${binary}" --version
  "${binary}" --help >/dev/null
}

if [[ "${1:-}" == "--binary" ]]; then
  smoke_binary "${2:?binary path required}"
  exit 0
fi

VERSION="${VERSION:?VERSION is required}"
DEB_ARCH="${DEB_ARCH:?DEB_ARCH is required}"
RPM_ARCH="${RPM_ARCH:?RPM_ARCH is required}"
DIST_DIR="${DIST_DIR:-dist}"

DEB="${DIST_DIR}/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb"
RPM="${DIST_DIR}/${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm"
TARBALL="${DIST_DIR}/${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}.tar.gz"

for artifact in "${DEB}" "${RPM}" "${TARBALL}"; do
  if [[ ! -f "${artifact}" ]]; then
    echo "Missing artifact: ${artifact}" >&2
    exit 1
  fi
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
tar -C "${WORKDIR}" -xzf "${TARBALL}"
smoke_binary "${WORKDIR}/${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}/${COMMAND_NAME}"

if [[ "${EUID}" -ne 0 ]]; then
  SUDO=(sudo)
else
  SUDO=()
fi

"${SUDO[@]}" apt-get install --yes "./${DEB}"
smoke_binary "$(command -v "${COMMAND_NAME}")"
"${SUDO[@]}" apt-get remove --yes "${PACKAGE_NAME}"

"${SUDO[@]}" rpm --install --nodeps "${RPM}"
smoke_binary "$(command -v "${COMMAND_NAME}")"
"${SUDO[@]}" rpm --erase "${PACKAGE_NAME}"
