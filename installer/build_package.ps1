# installer/build_package.ps1 -- run on Windows (CI or locally).
#
# Expects Python 3.12 on PATH. Produces dist\CrealityCFSBridge\ holding
# cfsbridge.exe (PyInstaller one folder), fluidd\ (the bundle), the renamed
# WinSW binary CrealityCFSBridge.exe and its CrealityCFSBridge.xml. The Inno
# Setup script, installer/CrealityCFSBridge.iss, packages that folder as it
# stands.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

# WinSW is pinned by URL and by content. A release asset that moved, was
# replaced, or came back truncated is a binary this installer would go on to
# ship as a Windows service host, so the hash is checked before it is used and
# the build stops rather than package something unrecognised.
$winswUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$winswSha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'

function Get-Sha256Hex([string] $Path) {
    # Get-FileHash is missing from some stock PowerShell installs, and a build
    # that skipped the check because the cmdlet was absent would be worse than
    # no check at all, so certutil (present on every supported Windows) is the
    # fallback rather than a warning.
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    }
    $out = & certutil.exe -hashfile $Path SHA256
    if ($LASTEXITCODE -ne 0) { throw "certutil could not hash $Path" }
    $hex = ($out | Where-Object { $_ -match '^[0-9a-fA-F ]+$' } |
            ForEach-Object { $_ -replace '\s', '' }) -join ''
    if ($hex.Length -ne 64) { throw "certutil gave no SHA-256 for $Path" }
    return $hex.ToLowerInvariant()
}

# $ErrorActionPreference = 'Stop' does not apply to a native command's exit
# code, so python failing here would otherwise be a warning on the console and
# a build that carried on to package whatever was left in dist from last time.
# Every python call below is therefore followed by its own check, naming the
# step, rather than relying on the preference.
python -m pip install --upgrade pip pyinstaller==6.* requests websockets
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed with exit code $LASTEXITCODE; nothing was packaged"
}
if (-not (Test-Path 'cfsbridge\fluidd\index.html')) { throw 'cfsbridge\fluidd is missing: build the Fluidd fork first (CI does this)' }

# --distpath and --workpath are explicit so the output lands next to this
# project whatever the spec or a future PyInstaller default decides.
python -m PyInstaller --clean --noconfirm --distpath dist --workpath build installer\cfsbridge.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE; nothing was packaged"
}

# Older Windows PowerShell defaults to TLS 1.0, which GitHub refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$winswOut = 'dist\CrealityCFSBridge\CrealityCFSBridge.exe'
Invoke-WebRequest -Uri $winswUrl -OutFile $winswOut -UseBasicParsing
$got = Get-Sha256Hex $winswOut
if ($got -ne $winswSha256) {
    Remove-Item $winswOut -Force -ErrorAction SilentlyContinue
    throw "WinSW SHA-256 mismatch: expected $winswSha256, got $got. Nothing should be packaged."
}
Write-Output "WinSW SHA-256 verified: $got"

Copy-Item installer\winsw.xml dist\CrealityCFSBridge\CrealityCFSBridge.xml

# The installer packs this folder, so the build states plainly that all
# four pieces are there instead of leaving a half-built folder to be found by
# the installer, or by a user.
foreach ($required in 'cfsbridge.exe', 'fluidd\index.html', 'CrealityCFSBridge.exe', 'CrealityCFSBridge.xml') {
    $path = Join-Path 'dist\CrealityCFSBridge' $required
    if (-not (Test-Path -LiteralPath $path)) { throw "the package is incomplete: $path is missing" }
}
Write-Output 'package contents verified'

Get-ChildItem dist\CrealityCFSBridge | Select-Object Name, Length
