$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
if (-not (Test-Path -LiteralPath 'config.json')) {
    & '.\.venv\Scripts\python.exe' configure.py
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось настроить доступ' }
}
& '.\.venv\Scripts\python.exe' app.py
