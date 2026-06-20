<#
  Rezo uninstaller.
  - removes the auto-start entry
  - optionally removes the virtualenv (-RemoveVenv) and collected data (-Purge)

  Quit the running app first via the tray icon (right-click -> Quit Rezo).
  Run:  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#>
param(
  [switch]$RemoveVenv,
  [switch]$Purge
)

$ErrorActionPreference = "SilentlyContinue"
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

Remove-ItemProperty -Path $regPath -Name "Rezo"
Write-Host "Removed auto-start entry."

if ($RemoveVenv) {
  Remove-Item -Recurse -Force (Join-Path $PSScriptRoot ".venv")
  Write-Host "Removed virtualenv."
}

if ($Purge) {
  $data = Join-Path $env:LOCALAPPDATA "Rezo"
  Remove-Item -Recurse -Force $data
  Write-Host "Removed collected data at $data."
}

Write-Host "Done. (If Rezo is still running, quit it from the tray icon.)"
