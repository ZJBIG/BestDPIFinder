$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv-autotest'
$python = $null
foreach ($candidate in @('py -3.13', 'py -3.12', 'py -3.11', 'python')) {
    try {
        $parts = $candidate -split ' '
        $probe = & $parts[0] @($parts | Select-Object -Skip 1) --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$probe" -match 'Python 3\.(11|12|13)') {
            $python = $parts
            break
        }
    } catch { }
}
if (-not $python) { throw 'No supported Python 3.11–3.13 installation was found (checked py launcher and python on PATH).' }
Write-Host "Using Python: $($python -join ' ')"
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    & $python[0] @($python | Select-Object -Skip 1) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create $venv (exit $LASTEXITCODE)." }
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-autotest.txt')
if ($LASTEXITCODE -ne 0) { throw "Installing autotest dependencies failed with exit $LASTEXITCODE." }
& $venvPython -c "import mss, PIL; print('mss', mss.__version__, '| Pillow', PIL.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Autotest dependency import check failed with exit $LASTEXITCODE." }
& $venvPython -m unittest discover -s (Join-Path $root 'tests') -p test_autotest_model.py -v
if ($LASTEXITCODE -ne 0) { throw "Autotest fast smoke failed with exit $LASTEXITCODE." }
& $venvPython (Join-Path $root 'autotest_seed_search.py') --max-seeds 10 --output (Join-Path $root 'autotest_seed.json')
if ($LASTEXITCODE -ne 0) { throw "Deterministic seed screening failed with exit $LASTEXITCODE; see the seed report output above." }
Write-Host 'Autotest environment setup and fast smoke test passed.' -ForegroundColor Green
