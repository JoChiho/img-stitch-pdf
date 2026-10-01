# Creates a Desktop .lnk that launches this app with pythonw (no console).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainPy = Join-Path $ProjectDir "main.py"

if (-not (Test-Path -LiteralPath $MainPy)) {
    Write-Error "main.py not found: $MainPy"
}

function Find-Pythonw {
    $candidates = @()
    if ($env:VIRTUAL_ENV) {
        $candidates += (Join-Path $env:VIRTUAL_ENV "Scripts\pythonw.exe")
    }
    try {
        $py = (Get-Command python -ErrorAction SilentlyContinue).Source
        if ($py) {
            $dir = Split-Path -Parent $py
            $candidates += (Join-Path $dir "pythonw.exe")
        }
    } catch {}
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\pythonw.exe",
        "C:\Python312\pythonw.exe",
        "C:\Python311\pythonw.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    $cmd = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) { return $pyCmd.Source }
    throw "pythonw.exe / python.exe not found. Install Python or activate the venv first."
}

$Pythonw = Find-Pythonw

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}
$LnkPath = Join-Path $Desktop "图片导出PDF.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($LnkPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$MainPy`""
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.WindowStyle = 1
$Shortcut.Description = "图片导出为多页 PDF"
$Shortcut.Save()

Write-Host "Created shortcut:"
Write-Host "  $LnkPath"
Write-Host "Target: $Pythonw"
Write-Host "Args:   `"$MainPy`""
Write-Host "Start in: $ProjectDir"
