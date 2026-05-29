// ==========================================================
// Python 실행을 uv 단일 환경(backend/.venv)으로 통일하는 헬퍼.
//  - 모든 spawn/execFile 은 `uv run --no-project --active` 로 실행
//  - VIRTUAL_ENV=backend/.venv 를 주입해 공용 환경을 사용
// ==========================================================
const path = require('path');

// 이 파일은 backend/src/services/ 에 위치 → ../../ = backend/
const BACKEND_DIR = path.join(__dirname, '..', '..');
const VENV_DIR = path.join(BACKEND_DIR, '.venv');
const VENV_PYTHON = path.join(
  VENV_DIR,
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'
);

// child_process 에 넘길 환경변수 (uv run --active 가 backend/.venv 를 쓰도록)
function uvEnv(extra = {}) {
  return { ...process.env, VIRTUAL_ENV: VENV_DIR, ...extra };
}

// uv 실행 커맨드 생성. pyArgs 예: ['uvicorn','server:app',...] 또는 ['script.py', ...]
function uvCommand(pyArgs) {
  return { cmd: 'uv', args: ['run', '--no-project', '--active', ...pyArgs] };
}

module.exports = { VENV_DIR, VENV_PYTHON, uvEnv, uvCommand };
