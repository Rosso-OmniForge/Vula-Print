# Vula! Print - Windows build script.
#
# Run from the repo root:
#     powershell -ExecutionPolicy Bypass -File windows\build.ps1
#
# Optional flags:
#     -SkipInstaller   Build VulaPrint.exe only (skip Inno Setup)
#     -SkipIcon        Reuse the existing VulaPrint.ico
#
# Produces:
#     dist\VulaPrint\VulaPrint.exe                          (portable)
#     windows\Output\VulaPrint-<version>-Setup.exe          (installer)
#
# Prerequisites (see windows\README.md):
#     python 3.11+   on PATH
#     pyinstaller    via:  pip install pyinstaller
#     pillow         via:  pip install -r requirements_app.txt
#     Inno Setup 6   from:  https://jrsoftware.org/isdl.php
#
# Optional env var:
#     VULA_CODE_SIGN_SUBJECT     subject name of the code-signing cert.
#                                If unset, installer is unsigned
#                                (SmartScreen will warn; see README.md).
#     VULA_SIGN_TIMESTAMP_URL    RFC 3161 timestamp URL.
#                                Default: http://timestamp.digicert.com

[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$SkipIcon
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Vula Print - Windows build" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Repo root: $RepoRoot"

# ---- Version ---------------------------------------------------------
$VersionFile = Join-Path $RepoRoot "VERSION"
if (-not (Test-Path $VersionFile)) {
    Write-Error "VERSION file not found at $VersionFile"
    exit 1
}
$AppVersion = (Get-Content $VersionFile -Raw).Trim()
if (-not $AppVersion) {
    Write-Error "VERSION file is empty"
    exit 1
}
Write-Host "  Version:   $AppVersion"

# ---- Python sanity ---------------------------------------------------
try {
    $pyVersion = & python --version 2>&1
    Write-Host "  Python:    $pyVersion"
} catch {
    Write-Error "Python not found on PATH. Install Python 3.11+ and add it to PATH."
    exit 1
}

& python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller not installed. Run:  pip install pyinstaller"
    exit 1
}

& python -c "from PIL import Image" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Pillow not installed. Run:  pip install -r requirements_app.txt"
    exit 1
}

# ---- Generate .ico ---------------------------------------------------
$IconPath = Join-Path $ScriptDir "VulaPrint.ico"
if (-not $SkipIcon) {
    $LogoPath = Join-Path $RepoRoot "assets\Vula_Logo.png"
    if (-not (Test-Path $LogoPath)) {
        Write-Error "assets\Vula_Logo.png not found - cannot generate .ico"
        exit 1
    }
    Write-Host "  Icon:      generating $IconPath"
    $IconHelper = Join-Path $ScriptDir "build_icon.py"
    & python $IconHelper $LogoPath $IconPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to generate VulaPrint.ico"
        exit 1
    }
} else {
    Write-Host "  Icon:      skipped (-SkipIcon)"
}

# ---- PyInstaller -----------------------------------------------------
Write-Host ""
Write-Host "-- PyInstaller --" -ForegroundColor Yellow
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }

& pyinstaller --clean --noconfirm windows\vula_print.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed (exit $LASTEXITCODE)"
    exit 1
}

$ExePath = Join-Path $RepoRoot "dist\VulaPrint\VulaPrint.exe"
if (-not (Test-Path $ExePath)) {
    Write-Error "PyInstaller produced no VulaPrint.exe at $ExePath"
    exit 1
}
$ExeSize = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
Write-Host "  VulaPrint.exe: $ExePath  ($ExeSize MB)"

if ($SkipInstaller) {
    Write-Host ""
    Write-Host "Skipping Inno Setup (-SkipInstaller)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Portable build ready. Try:"
    Write-Host "  $ExePath"
    exit 0
}

# ---- Inno Setup ------------------------------------------------------
Write-Host ""
Write-Host "-- Inno Setup --" -ForegroundColor Yellow

$IsccCandidates = @(
    "iscc.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$Iscc = $null
foreach ($cand in $IsccCandidates) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $Iscc = $cand
        break
    }
    if (Test-Path $cand) {
        $Iscc = $cand
        break
    }
}
if (-not $Iscc) {
    Write-Error "iscc.exe (Inno Setup 6 compiler) not found. Install Inno Setup 6 and re-run."
    exit 1
}
Write-Host "  iscc:      $Iscc"

& $Iscc "/DAppVersion=$AppVersion" "windows\vula_print.iss"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup failed (exit $LASTEXITCODE)"
    exit 1
}

$InstallerPath = Join-Path $ScriptDir "Output\VulaPrint-$AppVersion-Setup.exe"
if (-not (Test-Path $InstallerPath)) {
    Write-Error "Inno Setup produced no installer at $InstallerPath"
    exit 1
}
$SetupSize = [math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)
Write-Host "  Installer: $InstallerPath  ($SetupSize MB)"

# ---- Code signing (optional) ----------------------------------------
Write-Host ""
Write-Host "-- Code signing --" -ForegroundColor Yellow
$SignSubject = $env:VULA_CODE_SIGN_SUBJECT
if ($SignSubject) {
    $TimestampUrl = $env:VULA_SIGN_TIMESTAMP_URL
    if (-not $TimestampUrl) {
        $TimestampUrl = "http://timestamp.digicert.com"
    }
    Write-Host "  Signing with subject: $SignSubject"
    & signtool sign /n $SignSubject /tr $TimestampUrl /td sha256 /fd sha256 $InstallerPath
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "signtool failed (exit $LASTEXITCODE) - installer is unsigned"
    }
} else {
    Write-Warning "VULA_CODE_SIGN_SUBJECT not set - installer will be UNSIGNED."
    Write-Warning "  SmartScreen will show 'Windows protected your PC' on install."
    Write-Warning "  See windows\README.md 'Installing on Windows'."
}

# ---- Done ------------------------------------------------------------
if ($SignSubject) {
    $SignLabel = "signed"
} else {
    $SignLabel = "UNSIGNED"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Build complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Installer:  $InstallerPath"
Write-Host "  Signature:  $SignLabel"
Write-Host ""