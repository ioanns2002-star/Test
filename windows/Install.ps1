[CmdletBinding()]
param(
    [switch]$EnableStartup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Install.ps1 must run on Windows."
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or [string]::IsNullOrWhiteSpace($env:APPDATA)) {
    throw "This Windows user profile is missing LOCALAPPDATA or APPDATA."
}

$sourceExecutable = Join-Path $PSScriptRoot "AgentBridge.exe"
if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
    throw "AgentBridge.exe was not found beside Install.ps1. Run this from the packaged release folder; a GitHub source-code ZIP cannot install AgentBridge."
}

$installDirectory = Join-Path (Join-Path $env:LOCALAPPDATA "Programs") "AgentBridge"
$installedExecutable = Join-Path $installDirectory "AgentBridge.exe"
$startMenuDirectory = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startMenuShortcut = Join-Path $startMenuDirectory "AgentBridge.lnk"
$startupDirectory = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$startupShortcut = Join-Path $startupDirectory "AgentBridge.lnk"

New-Item -ItemType Directory -Force -Path $installDirectory, $startMenuDirectory | Out-Null
Copy-Item -LiteralPath $sourceExecutable -Destination $installedExecutable -Force

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($startMenuShortcut)
$shortcut.TargetPath = $installedExecutable
$shortcut.WorkingDirectory = $installDirectory
$shortcut.Description = "Visible AgentBridge desktop tray application"
$shortcut.Save()

if ($EnableStartup) {
    New-Item -ItemType Directory -Force -Path $startupDirectory | Out-Null
    $shortcut = $shell.CreateShortcut($startupShortcut)
    $shortcut.TargetPath = $installedExecutable
    $shortcut.WorkingDirectory = $installDirectory
    $shortcut.Description = "Start AgentBridge when this user signs in"
    $shortcut.Save()
    Write-Host "Created the optional per-user startup shortcut."
}
else {
    Write-Host "No startup shortcut was created. Re-run with -EnableStartup only if this user explicitly wants it."
}

Write-Host "Installed AgentBridge for this user: $installedExecutable"
Write-Host "Launch it from the Start menu. The first launch opens its visible local settings window."
Write-Host "This installer does not request elevation, change Defender/firewall settings, create a service, or handle secrets."
