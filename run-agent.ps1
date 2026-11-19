param(
    [string[]]$Arguments = @('run', '--max-applications', '10')
)

$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m src.cli @Arguments
