. (Join-Path $PSScriptRoot '_common.ps1')
# sar_server.py 는 backend/ 루트($RepoRoot)에 있음
Start-UvService `
    -WorkingDir $RepoRoot `
    -LogFile    (Join-Path $PSScriptRoot 'logs\sar-server-8005.log') `
    -Banner     'start sar-server on 8005' `
    -UvArgs     @('python', 'sar_server.py')
