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
brew install dataplicity-cli
dataplicity --help
```

### Windows (no Python required)

Download the latest `.msi` from [GitHub Releases](https://github.com/wildfoundry/dataplicity-cli/releases) and install it. It installs `dataplicity.exe` and adds it to `PATH`.

```
dataplicity --help
```

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
```

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
- The `Update Homebrew tap` workflow opens a PR in `wildfoundry/homebrew-tap` after each published release.
- Configure a repository secret named `HOMEBREW_TAP_TOKEN` (PAT with repo write access to `wildfoundry/homebrew-tap`) to enable that automation.
- The `Update WinGet package` workflow publishes new `.msi` releases to WinGet using `Wildfoundry.DataplicityCLI`.
- Configure a repository secret named `WINGET_TOKEN` (classic PAT with `public_repo`) and ensure your account has a fork of `microsoft/winget-pkgs`.
- WinGet automation updates existing manifests; if this package is not yet in WinGet, submit the first manifest manually, then subsequent releases are automated.
