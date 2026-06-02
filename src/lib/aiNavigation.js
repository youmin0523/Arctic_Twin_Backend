/**
 * aiNavigation.js
 *
 * AI 항해 통합 — 분리 운영되던 3개 AI 모듈(빙산 회피 RL·출항 스케줄링·연료 예측)의
 * 결과를 하나의 일관된 의사결정으로 집약하는 순수 로직(발표자료 통합 갭 #3).
 *
 * 각 모듈 호출은 실패할 수 있으므로(서비스 미기동 등) Promise.allSettled 결과를
 * 정규화한 뒤 결합한다. 일부 모듈만 살아있어도 graceful 하게 부분 결과를 제공한다.
 * I/O 는 라우트(ai.js)가 담당하고 이 모듈은 결합 규칙만 담는다(테스트 가능).
 */

/** Promise.allSettled 항목 또는 {ok,...} 를 표준 모듈 결과로 정규화 */
function normalizeModule(name, settled) {
  // settled: { status:'fulfilled', value } | { status:'rejected', reason } | {ok,data,error}
  if (settled && settled.status === 'fulfilled') {
    const v = settled.value;
    if (v && v.ok === false) return { name, ok: false, error: v.error || 'module error' };
    return { name, ok: true, data: v && 'data' in v ? v.data : v };
  }
  if (settled && settled.status === 'rejected') {
    return { name, ok: false, error: String(settled.reason && settled.reason.message || settled.reason || 'rejected') };
  }
  if (settled && typeof settled === 'object' && 'ok' in settled) {
    return settled.ok
      ? { name, ok: true, data: settled.data }
      : { name, ok: false, error: settled.error || 'module error' };
  }
  return { name, ok: false, error: 'no result' };
}

/**
 * 3개 AI 모듈 결과를 통합 항해 의사결정으로 결합.
 * @param {object} inputs - { avoidance, departure, fuel } 각각 allSettled 항목 또는 {ok,...}
 * @returns {object} 통합 결과
 */
function aggregateNavigation({ avoidance, departure, fuel } = {}) {
  const modules = {
    avoidance: normalizeModule('avoidance', avoidance),
    departure: normalizeModule('departure', departure),
    fuel: normalizeModule('fuel', fuel),
  };
  const order = ['avoidance', 'departure', 'fuel'];
  const availableNames = order.filter((k) => modules[k].ok);
  const available = availableNames.length;
  const degraded = available < order.length;

  // 통합 권고 문구 — 살아있는 모듈 기반으로 일관 요약
  const summaryParts = [];
  if (modules.avoidance.ok) summaryParts.push('회피 경로 산출');
  if (modules.departure.ok) summaryParts.push('출항 시점 평가');
  if (modules.fuel.ok) summaryParts.push('연료 예측');

  let status;
  if (available === 0) status = 'unavailable';
  else if (degraded) status = 'degraded';
  else status = 'ok';

  return {
    status,                       // 'ok' | 'degraded' | 'unavailable'
    available,                    // 살아있는 모듈 수 (0~3)
    total: order.length,
    degraded,
    modules,                      // 모듈별 상세
    unavailable: order.filter((k) => !modules[k].ok),
    summary: summaryParts.length
      ? `통합 AI 항해: ${summaryParts.join(' · ')} (${available}/${order.length} 모듈)`
      : '가용한 AI 모듈 없음',
  };
}

module.exports = { aggregateNavigation, normalizeModule };
