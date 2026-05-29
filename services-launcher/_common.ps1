# ==========================================================
# services-launcher 공통 헬퍼 — Python 실행을 uv 로 통일
#  - 모든 서비스가 단일 환경 backend/.venv 를 공유
#  - uv run --no-project --active 로 그 환경을 사용
#  - 각 런처는 이 파일을 dot-source 후 Start-UvService 호출
# ==========================================================
$ErrorActionPreference = 'Stop'

# $RepoRoot = backend/ (services-launcher 의 부모)
$script:RepoRoot     = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$script:VenvDir      = Join-Path $RepoRoot '.venv'
$script:Requirements = Join-Path $RepoRoot 'requirements.txt'

function Initialize-UvEnv {
    # backend/.venv 가 없으면 생성 + requirements 설치 (멱등).
    $py = Join-Path $VenvDir 'Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Write-Host "[uv] creating shared venv: $VenvDir"
        uv venv $VenvDir
        Write-Host "[uv] installing requirements: $Requirements"
        uv pip install --python $VenvDir -r $Requirements
    }
    # uv run --active 가 이 환경을 사용하도록 활성 표시
    $env:VIRTUAL_ENV = $VenvDir
}

function Start-UvService {
    param(
        [Parameter(Mandatory)][string]   $WorkingDir,
        [Parameter(Mandatory)][string]   $LogFile,
        [Parameter(Mandatory)][string]   $Banner,
        [Parameter(Mandatory)][string[]] $UvArgs
    )
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Initialize-UvEnv
    Set-Location $WorkingDir
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$ts] === $Banner ===" | Out-File -FilePath $LogFile -Append -Encoding utf8
    uv run --no-project --active @UvArgs *>> $LogFile
}
