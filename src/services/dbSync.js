/**
 * dbSync.js
 * =========
 * JSON(backend/data) → Neon PostgreSQL 동기화 트리거 (scripts/sync_db.js 실행).
 *
 * index.js(스케줄러/시작 시)와 collab.js(SAR 온디맨드 탐지 후)에서 공용으로 사용한다.
 * - 멱등(upsert)이라 반복 실행 안전. DATABASE_URL 없으면 skip(파일 폴백 모드).
 * - 동시 실행 방지: 실행 중 요청은 1회로 합쳐 끝난 뒤 재실행.
 * - 동기화 성공 후 읽기 캐시(dataStore TTL/mtime, sar 캐시)를 비워 최신 반영.
 */
const path = require('path');
const fs = require('fs');
const { execFile } = require('child_process');

const BACKEND_DIR = path.join(__dirname, '..', '..');
const SYNC_SCRIPT = path.join(BACKEND_DIR, 'scripts', 'sync_db.js');

let _running = false;
let _pending = false;

function _clearReadCaches() {
  try { require('./dataStore').clearCache(); } catch { /* noop */ }
  try { require('./sarDetectionStore').clearSarCache(); } catch { /* noop */ }
}

/**
 * sync_db.js 를 자식 프로세스로 실행. node 바이너리는 현재 실행 중인 것을 그대로
 * 사용(process.execPath)해 PATH 의존성을 제거한다.
 * @param {string} reason 로그용 사유 태그
 */
function runMigrate(reason = '') {
  if (!process.env.DATABASE_URL) return; // DB 미설정 → 파일 폴백 모드, 동기화 불필요
  if (!fs.existsSync(SYNC_SCRIPT)) {
    console.warn('[Migrate] scripts/sync_db.js 없음 — 동기화 건너뜀');
    return;
  }
  if (_running) { _pending = true; return; } // 실행 중이면 끝난 뒤 1회 재실행

  _running = true;
  console.log(`[Migrate] JSON → DB 동기화 시작${reason ? ` (${reason})` : ''}`);
  execFile(process.execPath, [SYNC_SCRIPT], { cwd: BACKEND_DIR, timeout: 300000 }, (err, stdout, stderr) => {
    if (err) console.error('[Migrate] 실패:', err.message);
    if (stdout) console.log('[Migrate]', stdout.trim().slice(-500));
    if (stderr) console.error('[Migrate] stderr:', stderr.trim().slice(-200));
    if (!err) _clearReadCaches(); // 성공 시에만 캐시 무효화 → 다음 조회가 최신 DB 반영
    _running = false;
    if (_pending) { _pending = false; runMigrate('pending'); }
  });
}

module.exports = { runMigrate };
