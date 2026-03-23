# Dataplicity CLI

A command line interface for Dataplicity (OEM/developer workflows).

## Install

### macOS (via brew + pipx)

```
brew install pipx
pipx install dataplicity-cli
```

### Linux (pipx recommended)

```
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install dataplicity-cli
```

## Quick start

Set the API base URL if you are not using the default:

```
dataplicity config set --base-url https://api.prelude.dataplicity.com
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
dataplicity auth api-key set
```

Show organisation:

```
dataplicity org show
```

List developer devices:

```
dataplicity devices list
```

Open a terminal:

```
dataplicity devices terminal <device-hash>
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

## Notes

- Secrets are never printed to stdout.
- `--json` outputs raw response data where available.
- The CLI is designed for OEM/developer access, not end users.
