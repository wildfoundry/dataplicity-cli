param(
  [Parameter(Mandatory=$true)]
  [string]$Version
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Dist = Join-Path $Root "dist"
$PyiDist = Join-Path $Root "pyinstaller-dist"
$Wxs = Join-Path $PSScriptRoot "DataplicityCLI.wxs"
$OutMsi = Join-Path $Dist ("dataplicity-cli-{0}-windows-x64.msi" -f $Version)

$Exe = Join-Path $PyiDist "dataplicity.exe"
if (!(Test-Path $Exe)) {
  throw "Expected $Exe. Build it first (pyinstaller)."
}

New-Item -ItemType Directory -Force -Path $Dist | Out-Null
Copy-Item $Exe (Join-Path $Dist "dataplicity.exe") -Force

# WiX v3 tools are expected to be on PATH (candle.exe, light.exe)
$parts = $Version.Split(".")
if ($parts.Length -eq 3) {
  $ProductVersion = "$Version.0"
} elseif ($parts.Length -eq 4) {
  $ProductVersion = $Version
} else {
  throw "Version '$Version' must be numeric like X.Y.Z or X.Y.Z.W for MSI packaging."
}

candle.exe -nologo -dProductVersion="$ProductVersion" -out (Join-Path $Dist "DataplicityCLI.wixobj") $Wxs
light.exe -nologo -ext WixUIExtension -out $OutMsi (Join-Path $Dist "DataplicityCLI.wixobj")

Write-Host ("Wrote {0}" -f $OutMsi)

