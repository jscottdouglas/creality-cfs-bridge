# installer/build_installer.ps1 -- run on Windows after build_package.ps1.
#
# Compiles installer\CrealityCFSBridge.iss into
# dist\CrealityCFSBridge_Setup_<version>.exe, stamping it with the version
# from installer\version.txt and the slicer release from
# installer\orca_release.json.
#
# -AllowPlaceholderSha exists for one purpose: checking that the script still
# compiles while orca_release.json is holding a placeholder checksum, before
# the slicer release has been built. A setup exe produced that way cannot
# download the slicer (Inno verifies the checksum and refuses), so it must
# never be published. Every real release build runs without the switch.
[CmdletBinding()]
param([switch] $AllowPlaceholderSha)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

function Find-Iscc {
    # INNO_DIR first so a portable unpack, which is what a build machine
    # without an installed Inno Setup has, wins over anything else present.
    $roots = @()
    if ($env:INNO_DIR) { $roots += $env:INNO_DIR }
    if (${env:ProgramFiles(x86)}) {
        $roots += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6')
    }
    foreach ($root in $roots) {
        $candidate = Join-Path $root 'ISCC.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw ("ISCC.exe was not found, so the installer cannot be compiled. " +
           "Install Inno Setup 6 (https://jrsoftware.org/isdl.php), or unpack " +
           "it somewhere and set INNO_DIR to the folder holding ISCC.exe. " +
           "Looked in: " + ($roots -join '; '))
}

# -Raw gives $null for an empty file, so the trim has to come after the null
# check rather than on the way out of Get-Content. The version reaches the
# output filename, AppVersion and VersionInfoVersion, and the last of those
# only accepts a numeric version, so it is checked for shape here where the
# message can say what is wrong.
$versionRaw = Get-Content installer\version.txt -Raw
if ($null -eq $versionRaw) { $versionRaw = '' }
$version = $versionRaw.Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw ("installer\version.txt must hold a three part version such as " +
           "0.3.0, not '$version'")
}

$orca = Get-Content installer\orca_release.json -Raw | ConvertFrom-Json
foreach ($field in 'url', 'asset', 'sha256') {
    if (-not $orca.$field) { throw "installer\orca_release.json has no $field" }
}

# The checksum is the only thing standing between a user and whatever happens
# to be at that URL, so a value that is not a SHA-256 stops the build here
# rather than shipping a setup exe whose download step is guaranteed to fail.
if ($orca.sha256 -notmatch '^[0-9a-fA-F]{64}$') {
    if (-not $AllowPlaceholderSha) {
        throw ("installer\orca_release.json sha256 is not 64 hex characters " +
               "($($orca.sha256)); fill in the real slicer checksum. Pass " +
               "-AllowPlaceholderSha only to check that the script compiles: " +
               "the result must not be published.")
    }
    Write-Warning ("compiling with a placeholder sha256 ($($orca.sha256)). " +
                   "This setup exe cannot download the slicer and must not be published.")
}

# The .iss packages this folder as build_package.ps1 leaves it. Checking the
# pieces here names the missing one, instead of letting ISCC fail on a
# wildcard that matched nothing or, worse, succeed with a partial folder.
# installer\write_orca_preset.py is on the list because it is the one file the
# installer's preset step needs and the one the spec has to bundle by hand: a
# package without it installs and starts and then fails at the last step.
foreach ($required in 'cfsbridge.exe', 'fluidd\index.html',
                      'CrealityCFSBridge.exe', 'CrealityCFSBridge.xml',
                      'installer\write_orca_preset.py') {
    $path = Join-Path 'dist\CrealityCFSBridge' $required
    if (-not (Test-Path -LiteralPath $path)) {
        throw "$path is missing: run installer\build_package.ps1 first"
    }
}

# The .iss reads this as ..\LICENSE, relative to installer\, for its
# LicenseFile. ISCC's own error for a missing one does not say which of the
# script's relative paths failed, so it is checked by name here.
if (-not (Test-Path -LiteralPath 'LICENSE')) {
    throw ("LICENSE is missing from the project root, which is the " +
           "..\LICENSE that installer\CrealityCFSBridge.iss compiles in")
}

$iscc = Find-Iscc
Write-Output "ISCC: $iscc"
& $iscc "/DVersion=$version" "/DOrcaUrl=$($orca.url)" "/DOrcaSha=$($orca.sha256)" `
        "/DOrcaAsset=$($orca.asset)" "/Odist" installer\CrealityCFSBridge.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$setup = "dist\CrealityCFSBridge_Setup_$version.exe"
if (-not (Test-Path -LiteralPath $setup)) {
    throw "ISCC reported success but $setup is not there"
}
Get-ChildItem $setup | Select-Object Name, Length
