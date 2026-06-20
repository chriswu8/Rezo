<#
  Rezo installer.
  - creates a local virtualenv and installs dependencies
  - registers Rezo to auto-start (hidden) on Windows login
  - launches it now

  Run from this folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
  Skip auto-start:        ... .\install.ps1 -NoAutostart
  Don't launch now:       ... .\install.ps1 -NoLaunch
#>
param(
  [switch]$NoAutostart,
  [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$proj   = $PSScriptRoot
$venv   = Join-Path $proj ".venv"
$py     = Join-Path $venv "Scripts\python.exe"
$pyw    = Join-Path $venv "Scripts\pythonw.exe"
$run    = Join-Path $proj "run.py"

Write-Host "Rezo installer" -ForegroundColor Cyan
Write-Host "Project: $proj"

# 1. virtualenv
if (-not (Test-Path $py)) {
  Write-Host "Creating virtualenv…"
  $base = (Get-Command py -ErrorAction SilentlyContinue)
  if ($base) { & py -3 -m venv $venv } else { & python -m venv $venv }
}

# 2. dependencies
Write-Host "Installing dependencies…"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r (Join-Path $proj "requirements.txt")

# 3. vendor Chart.js if missing (offline support)
$chart = Join-Path $proj "rezo\web\vendor\chart.umd.min.js"
if (-not (Test-Path $chart)) {
  Write-Host "Fetching Chart.js…"
  New-Item -ItemType Directory -Force -Path (Split-Path $chart) | Out-Null
  try {
    Invoke-WebRequest -UseBasicParsing `
      -Uri "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" `
      -OutFile $chart
  } catch { Write-Warning "Could not download Chart.js; charts need internet until vendored." }
}

# 4. auto-start on login (HKCU Run -> pythonw, no console window)
if (-not $NoAutostart) {
  $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
  $cmd = '"{0}" "{1}"' -f $pyw, $run
  Set-ItemProperty -Path $regPath -Name "Rezo" -Value $cmd
  Write-Host "Registered auto-start on login." -ForegroundColor Green
}

# 5. launch now
if (-not $NoLaunch) {
  Write-Host "Launching Rezo…" -ForegroundColor Green
  Start-Process -FilePath $pyw -ArgumentList "`"$run`"" -WorkingDirectory $proj
}

Write-Host ""
Write-Host "Done. Dashboard: http://127.0.0.1:8787/" -ForegroundColor Cyan
Write-Host "Tip: history fills in over time. To preview every view now:"
Write-Host "     & '$py' '$run' --seed   (then open the dashboard)"
