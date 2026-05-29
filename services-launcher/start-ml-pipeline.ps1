. (Join-Path $PSScriptRoot '_common.ps1')
Start-UvService `
    -WorkingDir (Join-Path $RepoRoot 'services\ml-pipeline') `
    -LogFile    (Join-Path $PSScriptRoot 'logs\ml-pipeline-8003.log') `
    -Banner     'start ml-pipeline on 8003' `
    -UvArgs     @('uvicorn', 'server:app', '--host', '127.0.0.1', '--port', '8003')
