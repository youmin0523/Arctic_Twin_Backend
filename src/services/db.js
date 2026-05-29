/**
 * db.js
 * =====
 * Neon PostgreSQL 단일 커넥션 풀.
 *
 * 정형 데이터(icebergs/bergs/sentinel1_products/sar_detections/simulation_results)는
 * 이 풀을 통해 조회한다. DATABASE_URL 미설정 또는 연결 실패 시 호출측이
 * 기존 backend/data/*.json 파일로 폴백하도록 hasDb()/query() 를 제공한다.
 *
 * 연결 문자열은 backend/.env 의 DATABASE_URL (database/.env 와 동일, Neon).
 * Neon 은 SSL 필수 → migrate.js 와 동일하게 ssl:{ rejectUnauthorized:false }.
 */
const { Pool } = require('pg');

const CONN = process.env.DATABASE_URL || null;

let pool = null;
if (CONN) {
  pool = new Pool({
    connectionString: CONN,
    ssl: { rejectUnauthorized: false },
    max: 5,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
  });
  pool.on('error', (err) => {
    console.error('[db] idle client error:', err.message);
  });
  console.log('[db] PostgreSQL pool 초기화 (Neon)');
} else {
  console.warn('[db] DATABASE_URL 미설정 — 모든 조회가 JSON 파일 폴백으로 동작합니다.');
}

/** DATABASE_URL 이 설정되어 풀이 살아있는지 여부. */
function hasDb() {
  return pool !== null;
}

/**
 * 파라미터화 쿼리 실행. 풀이 없으면 예외를 던져 호출측이 폴백하도록 한다.
 * @returns {Promise<import('pg').QueryResult>}
 */
async function query(text, params) {
  if (!pool) throw new Error('DATABASE_URL 미설정 (pool 없음)');
  return pool.query(text, params);
}

async function close() {
  if (pool) await pool.end();
}

module.exports = { hasDb, query, close };
