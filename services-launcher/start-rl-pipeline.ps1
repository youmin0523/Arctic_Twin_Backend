. (Join-Path $PSScriptRoot '_common.ps1')
Start-UvService `
    -WorkingDir (Join-Path $RepoRoot 'services\rl-pipeline') `
    -LogFile    (Join-Path $PSScriptRoot 'logs\rl-pipeline-8001.log') `
    -Banner     'start rl-pipeline on 8001' `
    -UvArgs     @('uvicorn', 'server:app', '--host', '127.0.0.1', '--port', '8001')
