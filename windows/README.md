# Vula Print — Windows

Windows build of the Vula Print desktop app. This directory contains the
packaging tooling; the Python source lives at the repo root and is shared
with the Linux build.

## Installing on Windows

The installer is **not code-signed**. On a fresh Windows machine, double-
clicking `VulaPrint-<version>-Setup.exe` will trigger Windows SmartScreen:

1. A blue dialog appears: *"Windows protected your PC — Microsoft Defender
   SmartScreen prevented an unrecognised app from starting."*
2. Click **More info**.
3. Click **Run anyway**.
4. The installer wizard appears — follow the prompts.

This is expected and deliberate. The company does not purchase
Microsoft's code-signing certificate. Signing is optional — see
[Building](#building) below for how to sign if a certificate ever
becomes available.

### Silent install (IT / automation)

```bat
VulaPrint-1.1.902-Setup.exe /SILENT /NoLogUpload /UpdateURL=https://example.com/vula/download
```

Parameters (all optional except where noted):

| Parameter | Effect |
|---|---|
| `/SILENT` | No installer UI. |
| `/VERYSILENT` | No UI, no progress window. |
| `/NoLogUpload` | Creates `%ProgramData%\Vula\log-upload-disabled` with Administrator-only write. Standard users cannot re-enable log upload for this machine. Equivalent to `install.sh --no-log-upload` on Linux. |
| `/UpdateURL=<url>` | Writes `%ProgramData%\Vula\update-url.txt`. The in-app **Update** button reads this file and shows the URL to the user. If omitted, the update dialog says "no update URL configured". |
| `/DIR=<path>` | Install to a non-default directory (default: `C:\Program Files\Vula Print`). |

### What the installer does

- Copies the app to `{Program Files}\Vula Print\` (requires admin)
- Registers an entry in **Add or Remove Programs**
- Creates a Start Menu shortcut
- Creates a machine-wide autostart shortcut:
  `{ProgramData}\Microsoft\Windows\Start Menu\Programs\StartUp\Vula Print.lnk`
- Creates `{ProgramData}\Vula\` with restricted ACL
  (`Administrators` full, `SYSTEM` full, `Users` read)
- Optionally writes the log-upload-disabled flag and update URL (see above)

### What the installer does **not** do

- It does not install drivers for your printer. Plug the printer in before
  running the app; Windows Update usually picks up CH340 / FTDI / PL2303
  drivers automatically. If the printer doesn't appear in the app's
  discovery list, install the manufacturer's driver first.
- It does not run the app automatically unless you leave the "Launch Vula
  Print" checkbox ticked on the last installer page.
- It does not configure the store connection. That happens on first launch
  via the onboarding wizard.
- It does not register a Windows Service. The app runs as a tray
  application launched by the autostart shortcut.

## Uninstalling

Use **Settings → Apps → Vula Print → Uninstall**, or
**Add or Remove Programs**. The uninstaller will:

- Stop any running Vula Print process
- Remove the app directory and all program files
- Remove the Start Menu and autostart shortcuts
- Remove `%ProgramData%\Vula\` (including the log-upload flag and update URL)
- Prompt whether to also remove `%APPDATA%\VulaPrint\` (your settings, API
  key, and print history) — choose **No** when reinstalling, **Yes** for a
  clean removal.

## Building

Prerequisites on the build machine:

- Windows 10 or 11 (x64)
- Python 3.11+ on PATH as `python`
- [Inno Setup 6](https://jrsoftware.org/isdl.php) installed with `iscc.exe`
  on PATH (the default install location is fine — the build script finds it)
- Run from a fresh clone of this repository:

```bat
pip install -r requirements_app.txt
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

The script:

1. Reads `VERSION` from the repo root
2. Generates `windows\VulaPrint.ico` from `assets\Vula_Logo.png`
3. Runs PyInstaller (`windows\vula_print.spec`) — produces `dist\VulaPrint\`
4. Runs Inno Setup (`windows\vula_print.iss`) — produces
   `windows\Output\VulaPrint-<version>-Setup.exe`
5. Optionally signs the installer (see below)

Build artefacts:

| Path | What it is |
|---|---|
| `dist\VulaPrint\` | Portable build (onedir) — run `VulaPrint.exe` directly, no install |
| `windows\Output\VulaPrint-<version>-Setup.exe` | The distributable installer |
| `windows\VulaPrint.ico` | Generated from `assets\Vula_Logo.png`; delete to regenerate |

### Code signing (optional)

If a code-signing certificate ever becomes available, set the environment
variable `VULA_CODE_SIGN_SUBJECT` to the certificate subject name before
running `build.ps1`:

```bat
set VULA_CODE_SIGN_SUBJECT=OmniForge Research
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

The script calls `signtool sign` with a timestamp URL (default:
`http://timestamp.digicert.com`, overridable via `VULA_SIGN_TIMESTAMP_URL`).
Without a certificate, the installer is left unsigned and SmartScreen will
warn on install.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| SmartScreen blocks the installer | Installer is unsigned | Click *More info* → *Run anyway* (see top of this file) |
| App doesn't launch at login | Autostart shortcut missing or blocked | Check `%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp\Vula Print.lnk` exists; some Group Policy settings disable Startup folder processing |
| Printer not detected | Missing USB-serial driver | Plug in the printer → Windows Update → check Device Manager for unknown USB device; install the manufacturer driver |
| App starts but no printers found | First launch, no scan yet | Click **Rescan Devices** on the Printers tab, or plug in the printer and wait 5 s |
| "Does the driver support RAW?" error on print | Printer driver rejects raw-byte submission | Install a generic / driver that supports raw pass-through, or use a USB-to-serial adapter and assign the COM port instead of the spooler queue |
| Update button says "no update URL configured" | Installer run without `/UpdateURL=` | Re-run installer with `/UpdateURL=<url>` (or edit `%ProgramData%\Vula\update-url.txt` directly) |
