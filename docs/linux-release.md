# Linux release runbook

This runbook covers the GitHub Release artifacts for Linux workstations:

- `dataplicity-cli_<version>_amd64.deb` / `dataplicity-cli_<version>_arm64.deb`
- `dataplicity-cli-<version>-1.x86_64.rpm` / `dataplicity-cli-<version>-1.aarch64.rpm`
- `dataplicity-cli-<version>-linux-x86_64.tar.gz` /
  `dataplicity-cli-<version>-linux-aarch64.tar.gz`
- `SHA256SUMS-linux-x86_64.txt` / `SHA256SUMS-linux-aarch64.txt`

The packages wrap the same PyInstaller onefile binary used by the macOS `.pkg`
and Windows `.msi`. Debian and RPM packaging uses `fpm`, matching Dataplicity
Lens. GitHub Releases remain the distribution channel; there is no apt or yum
repository in this release contract.

32-bit ARM is out of scope. The CLI is a workstation tool, so the matrix is
x86_64 and aarch64 only.

## Compatibility

Release binaries are built on Ubuntu GitHub runners and are glibc-linked.
Support Ubuntu 22.04+, Debian 12+, RHEL/Fedora 9+, and current openSUSE
Leap/Tumbleweed on x86_64 and aarch64. Older glibc hosts should use the
tarball only after confirming `ldd` on the extracted binary, or run from
source.

The packages install `/usr/bin/dataplicity` and do not require a local Python
runtime.

## Release checklist

1. Confirm the version in `pyproject.toml` and `dataplicity_cli/__init__.py`
   matches the intended `vX.Y.Z` tag.
2. Require green unit tests and the Linux packaging smoke (install `.deb`,
   `.rpm`, and `.tar.gz`; `--version` / `--help`; port-forward against the
   mock gateway).
3. Create and push the release tag only after the preceding checks pass.
4. Confirm the GitHub release contains the versioned Linux `.deb`, `.rpm`,
   `.tar.gz`, and checksum files for both architectures.
5. Download the published artifacts onto a clean Linux VM (Debian/Ubuntu for
   the `.deb`, Fedora/RHEL for the `.rpm`, or either for the tarball).
6. Verify checksums, then install and smoke:

   ```sh
   sha256sum --check --ignore-missing SHA256SUMS-linux-x86_64.txt
   sudo apt install ./dataplicity-cli_X.Y.Z_amd64.deb
   dataplicity --version
   dataplicity --help
   dataplicity doctor
   ```

7. On a non-production organisation, run the manual functional smoke below,
   including port forwarding.

## Manual Linux smoke

Use a non-production test organisation with one online Linux device.

- Install the `.deb` or `.rpm` and confirm `dataplicity` is on `PATH`.
- Run `dataplicity --version`, `dataplicity --help`, and `dataplicity doctor`.
- Authenticate, then `dataplicity devices list`.
- Run a harmless `dataplicity devices run`.
- Forward a known-open device port and confirm local traffic:

  ```sh
  dataplicity devices port-forward <device-hash> --remote-port 80 --local-port 8080
  curl -sS http://127.0.0.1:8080/
  ```

- Stop the forward with Ctrl-C, then uninstall and confirm `/usr/bin/dataplicity`
  is removed.

Packaged-binary port forwarding can be exercised without a live device using
the mock gateway:

```sh
python3 scripts/port_forward_mock_gateway.py --write-config /tmp/dp-cli.json
# in another terminal, after installing the package:
scripts/linux_port_forward_smoke.sh /usr/bin/dataplicity
```

To install the generated artifacts in a local Debian cloud VM and prove
port forwarding through QEMU:

```sh
./build/linux/build_packages.sh "$(python build/get_version.py)"
scripts/linux-vm-smoke.sh
```
