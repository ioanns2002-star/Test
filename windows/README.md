# AgentBridge for Windows

The Windows desktop app is a visible tray application. It does not start a
Windows service, elevate privileges, alter Defender/firewall settings, or start
automatically unless the user explicitly chooses the startup option below.

## Build a release bundle

Build on **64-bit Windows** with Python 3.11 or newer. Python is needed only on
the build machine; the generated executable bundles Python and its dependencies.

```powershell
py -3.11 -m pip install ".[windows,build]"
powershell -NoProfile -File .\windows\Build.ps1
```

Alternatively, after selecting the intended Python interpreter, let the script
install the pinned build dependencies:

```powershell
powershell -NoProfile -File .\windows\Build.ps1 -InstallBuildDependencies
```

`Build.ps1` requires PyInstaller 6.22.2 and produces:

- `dist\AgentBridge.exe` — portable Windows executable.
- `dist\AgentBridge-windows-x64.zip` — per-user release bundle containing the
  executable, `Install.ps1`, `Uninstall.ps1`, and this guide.
- `dist\SHA256SUMS.txt` — SHA-256 checksum of that ZIP.

Before producing the ZIP, the build script runs the packaged EXE's import,
DPAPI round-trip, image-library, and hidden Tk-window checks. This is not a live
tray interaction, game test, or code-signing certificate. The EXE is unsigned.

The ZIP made by GitHub's **Source code** download is source only. It is not an
executable installer and does not contain `AgentBridge.exe`.

## Install for one Windows user

Extract `AgentBridge-windows-x64.zip`, open the extracted folder, then run:

```powershell
powershell -NoProfile -File .\Install.ps1
```

This copies the executable to `%LOCALAPPDATA%\Programs\AgentBridge` and adds a
Start menu shortcut. It requires no administrator account and does not change
PowerShell execution policy. If local policy prevents the script from running,
use the organization-approved installation method rather than bypassing policy.

AgentBridge does **not** create a startup entry by default. To opt in for this
user only:

```powershell
powershell -NoProfile -File .\Install.ps1 -EnableStartup
```

Launch AgentBridge from the Start menu. On first launch its visible settings
window explains that, while access is enabled, the configured trusted controller
can run supported commands and desktop operations with the current Windows
account's rights. There is no automatic elevation and no per-command approval
prompt; pause, disconnect, or quit from the tray icon to withdraw access.

The relay URL is stored as local configuration metadata. The device token and
shared Fernet peer key are encrypted with the current user's Windows DPAPI in
`%LOCALAPPDATA%\AgentBridge\config.json`; long-lived local logs and operation
data are kept under `%LOCALAPPDATA%\AgentBridge\data`. Do not add either secret
to GitHub, a source ZIP, or a release archive.

## Uninstall

Quit AgentBridge, then run:

```powershell
powershell -NoProfile -File .\Uninstall.ps1
```

This removes the per-user program files and shortcuts while retaining local
settings and data. To deliberately remove the DPAPI-protected configuration and
local data as well, add `-RemoveProfile`.
