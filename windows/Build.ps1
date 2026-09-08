[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$InstallBuildDependencies
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Build.ps1 must run on Windows. The produced executable is a Windows desktop application."
}
if (-not [Environment]::Is64BitProcess) {
    throw "Use a 64-bit Python interpreter to build the x64 AgentBridge executable."
}

$windowsDirectory = $PSScriptRoot
$projectRoot = Split-Path -Parent $windowsDirectory
$distDirectory = Join-Path $projectRoot "dist"
$buildDirectory = Join-Path $projectRoot "build"
$entryPoint = Join-Path $windowsDirectory "agentbridge_desktop.py"

if ($InstallBuildDependencies) {
    Push-Location $projectRoot
    try {
        & $Python -m pip install --disable-pip-version-check --upgrade ".[windows,build]"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not install the Windows build dependencies."
        }
    }
    finally {
        Pop-Location
    }
}

$pythonVersion = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not run the selected Python interpreter: $Python"
}
if ([version]$pythonVersion -lt [version]"3.11") {
    throw "AgentBridge requires Python 3.11 or newer to build."
}

$pythonBits = (& $Python -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($LASTEXITCODE -ne 0 -or $pythonBits -ne "64") {
    throw "Use a 64-bit Python interpreter to build the x64 AgentBridge executable."
}

$pyInstallerVersion = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 6.22.2 is required. Run Build.ps1 -InstallBuildDependencies first."
}
if ($pyInstallerVersion -ne "6.22.2") {
    throw "Expected PyInstaller 6.22.2, found $pyInstallerVersion. Use the pinned build extra."
}

New-Item -ItemType Directory -Force -Path $distDirectory, $buildDirectory | Out-Null
$pyInstallerWorkDirectory = Join-Path $buildDirectory "pyinstaller"
$pyInstallerSpecDirectory = Join-Path $buildDirectory "spec"

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name "AgentBridge" `
    --paths $projectRoot `
    --distpath $distDirectory `
    --workpath $pyInstallerWorkDirectory `
    --specpath $pyInstallerSpecDirectory `
    --hidden-import agentbridge.agent `
    --hidden-import tkinter `
    --hidden-import tkinter.ttk `
    --hidden-import tkinter.messagebox `
    --hidden-import tkinter.scrolledtext `
    --hidden-import pystray `
    --hidden-import pystray._win32 `
    --hidden-import PIL.Image `
    --hidden-import PIL.ImageDraw `
    $entryPoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller could not build AgentBridge.exe."
}

$executable = Join-Path $distDirectory "AgentBridge.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "PyInstaller completed without producing AgentBridge.exe."
}

$selfTestReport = Join-Path $distDirectory "self-test.json"
& $Python -c "import subprocess, sys; subprocess.run([sys.argv[1], '--self-test', sys.argv[2]], check=True, timeout=60)" $executable $selfTestReport
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $selfTestReport) {
        Get-Content -LiteralPath $selfTestReport | Write-Host
    }
    throw "The packaged executable failed its Windows self-test."
}
Write-Host "Packaged executable passed its import, DPAPI, Pillow, and Tk self-tests."

$bundleName = "AgentBridge-windows-x64"
$bundleDirectory = Join-Path $distDirectory $bundleName
$zipPath = Join-Path $distDirectory "$bundleName.zip"
if (Test-Path -LiteralPath $bundleDirectory) {
    Remove-Item -LiteralPath $bundleDirectory -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
New-Item -ItemType Directory -Path $bundleDirectory | Out-Null

Copy-Item -LiteralPath $executable -Destination (Join-Path $bundleDirectory "AgentBridge.exe")
foreach ($fileName in @("Install.ps1", "Uninstall.ps1", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $windowsDirectory $fileName) -Destination $bundleDirectory
}
Compress-Archive -LiteralPath $bundleDirectory -DestinationPath $zipPath -CompressionLevel Optimal

$checksum = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $distDirectory "SHA256SUMS.txt") -Value "$checksum  $bundleName.zip" -Encoding ascii

Write-Host "Built portable executable: $executable"
Write-Host "Built per-user release bundle: $zipPath"
Write-Host "The release bundle contains AgentBridge.exe and installer scripts; a GitHub source-code ZIP does not."
