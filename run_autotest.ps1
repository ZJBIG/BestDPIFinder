$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv-autotest\Scripts\python.exe'
if (-not (Test-Path $python)) {
    & (Join-Path $root 'setup_autotest.ps1')
    if ($LASTEXITCODE -ne 0) { throw "Autotest setup failed with exit $LASTEXITCODE." }
}
& $python (Join-Path $root 'autotest_seed_search.py') --max-seeds 10
if ($LASTEXITCODE -ne 0) { throw "Deterministic demo seed search failed with exit $LASTEXITCODE." }
$seedFile = Join-Path $root 'autotest_seed.json'
$seed = (Get-Content $seedFile -Raw | ConvertFrom-Json).seed
Write-Host "Starting real-time Windows QA run with selected seed $seed (speed 1.0)."
& $python (Join-Path $root 'main.py') --autotest --seed $seed --autotest-output (Join-Path $root 'autotest_output')
if ($LASTEXITCODE -ne 0) { throw "Real-time BestDPI Finder autotest failed with exit $LASTEXITCODE." }
$zip = Join-Path $root 'BestDPIFinder_Autotest_Result.zip'
if (-not (Test-Path $zip)) { throw "Autotest ended without creating expected archive: $zip" }
Write-Host "QA run succeeded. ZIP: $zip" -ForegroundColor Green
