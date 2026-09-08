[CmdletBinding()]
param(
    [switch]$RemoveProfile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Uninstall.ps1 must run on Windows."
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or [string]::IsNullOrWhiteSpace($env:APPDATA)) {
    throw "This Windows user profile is missing LOCALAPPDATA or APPDATA."
}

$installDirectory = Join-Path (Join-Path $env:LOCALAPPDATA "Programs") "AgentBridge"
$installedExecutable = Join-Path $installDirectory "AgentBridge.exe"
$startMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AgentBridge.lnk"
$startupShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\AgentBridge.lnk"
$profileDirectory = Join-Path $env:LOCALAPPDATA "AgentBridge"

function Remove-AgentBridgeShortcut {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($Path)
        if ($shortcut.TargetPath -ieq $installedExecutable) {
            Remove-Item -LiteralPath $Path -Force
        }
    }
    catch {
        Write-Warning "Could not inspect shortcut: $Path"
    }
}

Remove-AgentBridgeShortcut -Path $startMenuShortcut
Remove-AgentBridgeShortcut -Path $startupShortcut

if (Test-Path -LiteralPath $installDirectory) {
    Remove-Item -LiteralPath $installDirectory -Recurse -Force
    Write-Host "Removed the per-user AgentBridge program files."
}
else {
    Write-Host "No per-user AgentBridge program files were found."
}

if ($RemoveProfile) {
    if (Test-Path -LiteralPath $profileDirectory) {
        Remove-Item -LiteralPath $profileDirectory -Recurse -Force
        Write-Host "Removed the local AgentBridge profile, including DPAPI-protected settings and local data."
    }
}
else {
    Write-Host "Kept $profileDirectory so local settings and data remain available. Use -RemoveProfile to delete them deliberately."
}
