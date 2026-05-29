. (Join-Path $PSScriptRoot '_common.ps1')
Start-UvService `
    -WorkingDir (Join-Path $RepoRoot 'services\report-service') `
    -LogFile    (Join-Path $PSScriptRoot 'logs\report-service-8002.log') `
    -Banner     'start report-service on 8002' `
    -UvArgs     @('uvicorn', 'server:app', '--host', '127.0.0.1', '--port', '8002')
