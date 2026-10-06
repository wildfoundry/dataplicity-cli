# Dataplicity CLI

A command line interface for Dataplicity (OEM/developer workflows).

## Install

### macOS (no Python required)

Download the latest `.pkg` from [GitHub Releases](https://github.com/wildfoundry/dataplicity-cli/releases) and install it. It installs `dataplicity` into `/usr/local/bin`.

```
dataplicity --help
```

### macOS (Homebrew, recommended for devs)

```
brew tap wildfoundry/tap
brew trust --formula wildfoundry/tap/dataplicity-cli
brew install wildfoundry/tap/dataplicity-cli
dataplicity --help
```

### Windows (no Python required)

Install the signed x64 MSI from WinGet in PowerShell or Windows Terminal:

```powershell
winget install --id Wildfoundry.DataplicityCLI --exact
dataplicity --help
```

WinGet handles future upgrades and uninstall:

```powershell
winget upgrade --id Wildfoundry.DataplicityCLI --exact
winget uninstall --id Wildfoundry.DataplicityCLI --exact
```

The installer is machine-wide and may request administrator approval. If
WinGet is unavailable, download the latest signed `.msi` from
[GitHub Releases](https://github.com/wildfoundry/dataplicity-cli/releases).
Both install paths add `dataplicity.exe` to `PATH`; open a new terminal after
installation.

### Linux (no Python required)

Download the `.deb`, `.rpm`, or `.tar.gz` matching your CPU from
[GitHub Releases](https://github.com/wildfoundry/dataplicity-cli/releases).
Debian/Ubuntu use the `.deb`; Fedora, RHEL, and openSUSE use the `.rpm`. Other
distributions can unpack the tarball. Checksums are published next to each
artifact as `SHA256SUMS-linux-x86_64.txt` or `SHA256SUMS-linux-aarch64.txt`.

```sh
# Debian / Ubuntu
sudo apt install ./dataplicity-cli_<version>_amd64.deb

# Fedora / RHEL / openSUSE
sudo dnf install ./dataplicity-cli-<version>-1.x86_64.rpm

# Other Linux
tar -xzf dataplicity-cli-<version>-linux-x86_64.tar.gz
sudo install -m 0755 dataplicity-cli-<version>-linux-x86_64/dataplicity /usr/local/bin/dataplicity

dataplicity --help
```

Use the `arm64` / `aarch64` artifacts on ARM workstations. The packages install
`dataplicity` into `/usr/bin`. See [`docs/linux-release.md`](docs/linux-release.md)
for the supported distro and glibc range.

### Python (developer install)

If you do have Python available and prefer `pipx`:

```
pipx install dataplicity-cli
```

## Quick start

Guided setup (recommended):

```
dataplicity setup
```

Quick health check:

```
dataplicity doctor
```

Set the API base URL if you are not using the default:

```
dataplicity config set --base-url https://gateway.dataplicity.com
```

Login with email/password:

```
dataplicity auth login --email you@example.com
dataplicity auth login --email you@example.com --mfa-code 123456
```

If the account uses MFA, pass `--mfa-code` for an authenticator app, or complete
security-key / MFA sign-in in the browser when the CLI opens a loopback login URL.

SSO login:

```
dataplicity auth sso --email you@example.com
```

Use an organisation API key:

```
dataplicity auth api-key
```

Show organisation:

```
dataplicity org show
```

List developer devices:

```
dataplicity devices list
```

Short alias:

```
dataplicity devices ls
```

Top-level shortcut:

```
dataplicity ls
```

Open a terminal:

```
dataplicity devices terminal <device-hash>
```

Run one command and capture output:

```
dataplicity devices run <device-hash> --command "uname -a"
```

Long-running command with no timeout:

```
dataplicity devices run <device-hash> --command "tail -f /var/log/syslog" --no-timeout
```

If you omit `<device-hash>`, the CLI now shows an interactive picker.

Top-level shortcut:

```
dataplicity connect
```

Forward device port 22 to local port 2022:

```
dataplicity devices port-forward <device-hash> --remote-port 22 --local-port 2022
```

Read a remote file:

```
dataplicity devices remote-file <device-hash> --path /etc/os-release --output ./os-release.txt
```

Output JSON for scripting:

```
dataplicity --json org show
```

List endpoint monitors:

```
dataplicity endpoint-monitors list
```

Inspect user impact:

```
dataplicity user-impact list --unresolved-only
```

Run a fleet job:

```
dataplicity fleet-jobs run --data '{"name":"restart-edge","device_hashes":["abc123"],"command":"restart"}'
```

Query logs:

```
dataplicity logging list --device <device-hash> --level error --since 4h
```

Discover web-style path filters for logs:

```
dataplicity logging path-map
```

Search logs across your org:

```
dataplicity logging list --search timeout --since 30m
```

Log output is client-truncated for safety (defaults to 150 recent lines when unfiltered, max 1000 records per request, long fields abbreviated).

Show your current session and fleet summary:

```
dataplicity whoami
```

Install shell completion:

```
dataplicity --install-completion zsh
```

## Notes

- Secrets are never printed to stdout.
- `--json` outputs raw response data where available.
- The CLI is designed for OEM/developer access, not end users.

## Maintainers

- Releases publish a macOS tarball (`dataplicity-cli-<version>-macos-universal2.tar.gz`) for Homebrew consumption.
- The `Update Homebrew tap` workflow opens a PR in your tap repo after each published release.
- Configure a repository secret named `HOMEBREW_TAP_TOKEN` (PAT with repo write access to your tap repository) to enable that automation.
- Configure repository variables to target your tap:
  - `HOMEBREW_TAP_REPOSITORY` (example: `your-org/homebrew-tap`)
  - `HOMEBREW_FORMULA_NAME` (default: `dataplicity-cli`)
- You can also override tap settings per manual run using workflow inputs `tap_repository` and `formula_name`.
- The `Update WinGet package` workflow publishes new `.msi` releases to WinGet using `Wildfoundry.DataplicityCLI`.
- Configure a repository secret named `WINGET_TOKEN` (classic PAT with `public_repo`) and ensure your account has a fork of `microsoft/winget-pkgs`.
- WinGet automation updates existing manifests; if this package is not yet in WinGet, submit the first manifest for the current release, then subsequent releases are automated.
- Follow [`docs/windows-release.md`](docs/windows-release.md) before tagging a Windows release or submitting its first WinGet manifest.
- Releases publish Linux `.deb`, `.rpm`, and `.tar.gz` artifacts for x86_64 and aarch64, plus `SHA256SUMS-linux-*.txt`.
- Follow [`docs/linux-release.md`](docs/linux-release.md) before tagging a Linux release.
