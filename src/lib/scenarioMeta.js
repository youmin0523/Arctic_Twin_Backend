/**
 * scenarioMeta.js
 *
 * 시뮬레이션 시나리오 파일명 ↔ 메타데이터 파싱.
 * 기존에는 simulations.js 라우트 핸들러 안에 정규식이 인라인으로 박혀 있어
 * 테스트가 불가능했다. 순수 함수로 추출해 계약(스키마) 테스트 대상으로 삼는다.
 *
 * 파일명 규칙: <route>_month<MM>_arc<N>.json  (예: nsr_month01_arc4.json)
 */

const SCENARIO_RE = /([a-z]+)_month(\d+)_arc(\d+)/i;

/**
 * 시나리오 stem(확장자 제외)에서 메타데이터를 파싱.
 * @param {string} stem - 예: 'nsr_month01_arc4'
 * @returns {{scenario: string, route_code: string|null, month: number|null, arc_level: number|null}}
 */
function parseScenarioName(stem) {
  const m = typeof stem === 'string' ? stem.match(SCENARIO_RE) : null;
  return {
    scenario: stem,
    route_code: m ? m[1].toUpperCase() : null,
    month: m ? Number(m[2]) : null,
    arc_level: m ? Number(m[3]) : null,
  };
}

/**
 * 시뮬레이션 payload 가 기대 스키마를 만족하는지 검증.
 * @param {*} payload
 * @returns {{ok: boolean, errors: string[]}}
 */
function validateSimulationPayload(payload) {
  const errors = [];
  if (!payload || typeof payload !== 'object') {
    return { ok: false, errors: ['payload 가 객체가 아님'] };
  }
  for (const key of ['metadata', 'ticks', 'summary']) {
    if (!(key in payload)) errors.push(`필수 키 누락: ${key}`);
  }
  if (payload.metadata && typeof payload.metadata.route !== 'string') {
    errors.push('metadata.route 가 문자열이 아님');
  }
  if (!Array.isArray(payload.ticks)) {
    errors.push('ticks 가 배열이 아님');
  } else if (payload.ticks.length > 0) {
    const t0 = payload.ticks[0];
    if (!t0 || !t0.ship || !t0.ship.position) {
      errors.push('ticks[0].ship.position 누락');
    }
  }
  if (payload.summary && typeof payload.summary.completed !== 'boolean') {
    errors.push('summary.completed 가 불리언이 아님');
  }
  return { ok: errors.length === 0, errors };
}

module.exports = { parseScenarioName, validateSimulationPayload, SCENARIO_RE };
