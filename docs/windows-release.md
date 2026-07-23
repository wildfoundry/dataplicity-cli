# Windows release runbook

This runbook covers the x64, per-machine WiX MSI published as
`Wildfoundry.DataplicityCLI`. Windows arm64 and portable/MSIX packages are not
part of the current release scope.

Do not submit v0.1.6 to WinGet. Its release uploaded the MSI without the
external WiX cabinet, so the package does not contain the executable payload.
The first WinGet version must be v0.1.7 or newer and must pass the MSI install
smoke that verifies the embedded payload.

## Distribution metadata

- Product and command: `Dataplicity CLI` / `dataplicity`
- Package identifier: `Wildfoundry.DataplicityCLI`
- Installer: WiX MSI, x64, per-machine
- License: `BSD-3-Clause`
- Copyright holder: `Wildfoundry Ltd`
- Current MSI and WinGet publisher: `Dataplicity`

Before the first public WinGet submission, Legal or Product must confirm that
`Dataplicity` is the intended public publisher name. Ops must also confirm that
it is consistent with the Azure Artifact Signing certificate subject. Record
the approval in the release issue.

## Signing configuration

The `Release` workflow uses GitHub OIDC and Azure Artifact Signing. Do not
export a private key or store a PFX file in GitHub.

The protected GitHub environment `release-signing` must provide:

- Secret: `AZURE_CLIENT_ID`
- Variables: `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- Variables: `AZURE_ARTIFACT_SIGNING_ENDPOINT`
- Variables: `AZURE_ARTIFACT_SIGNING_ACCOUNT`,
  `AZURE_ARTIFACT_SIGNING_PROFILE`

Ops owns the Azure signing account and must document internally:

- primary and backup owner;
- certificate/profile expiry date and renewal reminder;
- who can approve the `release-signing` environment;
- recovery steps for a failed or unavailable signing profile.

Repository access only reveals secret and variable names, never their values.
Validate the configuration by running a release build and checking both
signatures rather than copying values into a ticket.

## Release checklist

1. Confirm the version in `pyproject.toml` and `dataplicity_cli/__init__.py`
   matches the intended `vX.Y.Z` tag.
2. Require green unit tests, Windows unit tests, and Windows MSI smoke.
3. Complete the manual Windows functional smoke below.
4. Create and push the release tag only after the preceding checks pass.
5. Confirm the GitHub release contains the versioned x64 MSI and
   `SHA256SUMS-windows-x64.txt`.
6. Download the published MSI to a clean Windows 10 or 11 VM.
7. Verify the MSI and installed executable:

   ```powershell
   Get-AuthenticodeSignature .\dataplicity-cli-X.Y.Z-windows-x64.msi
   Get-FileHash .\dataplicity-cli-X.Y.Z-windows-x64.msi -Algorithm SHA256
   ```

   Both signatures must report `Valid`; the hash must match the release
   checksum.
8. Record the OS version, artifact hash, signing subject, timestamp, and smoke
   results in the release issue.

## Manual Windows smoke

Use a non-production test organisation with one online Linux device and one
offline device.

- Install the MSI interactively and with `/qn`; verify `dataplicity` is on PATH
  in a new PowerShell process.
- Run `dataplicity --version`, `dataplicity --help`, and `dataplicity doctor`.
- Test password login or MFA where enabled, browser SSO, token refresh,
  `whoami`, and logout.
- Run `dataplicity devices list` and inspect both online and offline devices.
- Run a harmless command with `dataplicity devices run`.
- Open and close `dataplicity devices terminal`.
- Test `dataplicity devices ssh` with a valid key, a missing key, and a bad key.
- Confirm an offline device fails promptly with an actionable error.
- Upgrade from the previous MSI, then uninstall silently; verify the executable
  and machine PATH entry are removed.

Do not publish to WinGet if signing is invalid or a core smoke item fails.
Document any accepted deferral with an owner, reason, and target release.
