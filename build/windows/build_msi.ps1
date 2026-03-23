param(
  [Parameter(Mandatory=$true)]
  [string]$Version
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Dist = Join-Path $Root "dist"
$Wxs = Join-Path $PSScriptRoot "DataplicityCLI.wxs"
$OutMsi = Join-Path $Dist ("dataplicity-cli-{0}-windows-x64.msi" -f $Version)

$Exe = Join-Path $Dist "dataplicity.exe"
if (!(Test-Path $Exe)) {
  throw "Expected $Exe. Build it first (pyinstaller)."
}

New-Item -ItemType Directory -Force -Path $Dist | Out-Null

# WiX v3 tools are expected to be on PATH (candle.exe, light.exe)
candle.exe -nologo -dVersion=$Version -out (Join-Path $Dist "DataplicityCLI.wixobj") $Wxs
light.exe -nologo -ext WixUIExtension -out $OutMsi (Join-Path $Dist "DataplicityCLI.wixobj")

Write-Host ("Wrote {0}" -f $OutMsi)

