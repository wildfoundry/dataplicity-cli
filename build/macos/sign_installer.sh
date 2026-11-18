#!/usr/bin/env bash
set -euo pipefail

# The release workflow calls this on an ephemeral macOS runner, before checksumming.
package="${1:?expected package path}"
[[ -f "$package" ]]
: "${MAC_INSTALLER_P12_BASE64:?Missing Developer ID Installer certificate in the release-signing environment}"
: "${MAC_INSTALLER_P12_PASSWORD:?Missing Developer ID Installer certificate password}"

export LC_ALL=C
umask 077
work=$(mktemp -d)
keychain="$work/installer.keychain-db"
cleanup() {
  security delete-keychain "$keychain" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
identity='Developer ID Installer: MachineForest Ltd (82FBZ5BYTV)'
keychain_password=$(openssl rand -hex 32)
security create-keychain -p "$keychain_password" "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
printf '%s' "$MAC_INSTALLER_P12_BASE64" | base64 -D > "$work/installer.p12"
security import "$work/installer.p12" -k "$keychain" -P "$MAC_INSTALLER_P12_PASSWORD" -T /usr/bin/productsign
rm "$work/installer.p12"
unset MAC_INSTALLER_P12_BASE64 MAC_INSTALLER_P12_PASSWORD
security set-key-partition-list -S apple-tool:,apple: -s -k "$keychain_password" "$keychain" >/dev/null

productsign --timestamp --keychain "$keychain" --sign "$identity" "$package" "$work/signed.pkg"
signature=$(pkgutil --check-signature "$work/signed.pkg")
printf '%s\n' "$signature"
grep -Eq '^[[:space:]]*Status: signed by (a developer certificate issued by Apple for distribution|a certificate trusted by )' <<< "$signature"
grep -Fq "1. $identity" <<< "$signature"
mv "$work/signed.pkg" "$package"
