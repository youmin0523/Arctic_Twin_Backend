/**
 * avoidanceLog.js
 *
 * RL 회피 메트릭 영속화의 순수 로직 — 스냅샷 검증/정규화 + 로그 집계.
 * (라우트는 이 함수들을 사용해 data/rl_avoidance_log.jsonl 에 JSONL 로 적재한다.)
 *
 * 프론트엔드 회피 컨트롤러의 getMetrics() 스냅샷은 세션이 끝나면 사라진다.
 * 이를 백엔드에 누적해 세션 간 추세(평균 RL 성공률·임박도 등)를 분석 가능하게 한다.
 */

const NUMERIC_FIELDS = [
  'checks', 'threats', 'applied', 'kept',
  'rlAttempts', 'rlSuccess', 'astarFallback',
  'rlSuccessRate', 'fallbackRate', 'avgConfidence',
];

/**
 * 클라이언트 스냅샷을 검증하고 로그 레코드로 정규화.
 * @param {*} body - 회피 메트릭 스냅샷 + 선택 메타(route, iceClass)
 * @param {string} ts - ISO 타임스탬프 (호출자가 주입 — 순수성 유지)
 * @returns {{ok: boolean, record?: object, errors?: string[]}}
 */
function normalizeSnapshot(body, ts) {
  const errors = [];
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, errors: ['snapshot must be an object'] };
  }
  if (!Number.isFinite(body.threats)) errors.push('threats must be a number');
  if (!Number.isFinite(body.checks)) errors.push('checks must be a number');
  // 비율 필드는 [0,1] 범위 검증(있을 때만)
  for (const k of ['rlSuccessRate', 'fallbackRate', 'avgConfidence']) {
    if (body[k] != null && (!Number.isFinite(body[k]) || body[k] < 0 || body[k] > 1)) {
      errors.push(`${k} must be within [0,1]`);
    }
  }
  if (errors.length) return { ok: false, errors };

  const record = { ts: ts || null };
  for (const k of NUMERIC_FIELDS) {
    if (Number.isFinite(body[k])) record[k] = body[k];
  }
  record.minTcpaHours = Number.isFinite(body.minTcpaHours) ? body.minTcpaHours : null;
  record.avgTcpaHours = Number.isFinite(body.avgTcpaHours) ? body.avgTcpaHours : null;
  if (typeof body.route === 'string') record.route = body.route;
  if (typeof body.iceClass === 'string') record.iceClass = body.iceClass;
  if (body.byMethod && typeof body.byMethod === 'object') record.byMethod = body.byMethod;
  return { ok: true, record };
}

/**
 * JSONL 레코드 배열을 세션 간 추세로 집계.
 * @param {object[]} records
 * @returns {object} 집계 통계
 */
function summarize(records) {
  const valid = (records || []).filter((r) => r && typeof r === 'object');
  const n = valid.length;
  if (n === 0) {
    return {
      sessions: 0, total_threats: 0, total_applied: 0,
      avg_rl_success_rate: null, avg_fallback_rate: null,
      avg_confidence: null, min_tcpa_hours: null,
    };
  }
  const sum = (key) => valid.reduce((a, r) => a + (Number.isFinite(r[key]) ? r[key] : 0), 0);
  const mean = (key) => {
    const xs = valid.filter((r) => Number.isFinite(r[key]));
    return xs.length ? round4(xs.reduce((a, r) => a + r[key], 0) / xs.length) : null;
  };
  const tcpas = valid.filter((r) => Number.isFinite(r.minTcpaHours)).map((r) => r.minTcpaHours);
  return {
    sessions: n,
    total_threats: sum('threats'),
    total_applied: sum('applied'),
    avg_rl_success_rate: mean('rlSuccessRate'),
    avg_fallback_rate: mean('fallbackRate'),
    avg_confidence: mean('avgConfidence'),
    min_tcpa_hours: tcpas.length ? round4(Math.min(...tcpas)) : null,
  };
}

/** JSONL 텍스트를 레코드 배열로 파싱 (깨진 줄은 건너뜀) */
function parseJsonl(text) {
  return (text || '')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      try {
        return JSON.parse(l);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function round4(x) {
  return Math.round(x * 10000) / 10000;
}

module.exports = { normalizeSnapshot, summarize, parseJsonl };
