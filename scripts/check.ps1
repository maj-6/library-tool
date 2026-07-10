# Local pre-push gate: the same checks CI runs (ruff + pytest).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python3 -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python3 -m pytest -q
exit $LASTEXITCODE
