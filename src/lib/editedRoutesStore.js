/**
 * editedRoutesStore.js
 *
 * 사용자 편집 항로의 검증·변환 순수 로직.
 * 기존엔 파일(JSON)만 저장했으나, PostgreSQL 영속화(다중사용자·감사) 시에도
 * 동일한 검증·행↔객체 변환을 재사용하도록 분리한다(테스트 가능).
 */

/**
 * 편집 항로 페이로드 검증. 형태: { routeKey: [{lon,lat,label?}, ...], ... }
 * @returns {{ok: boolean, errors?: string[]}}
 */
function validateRoutesPayload(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, errors: ['payload must be an object keyed by route'] };
  }
  const errors = [];
  for (const [k, v] of Object.entries(body)) {
    if (!Array.isArray(v)) {
      errors.push(`route ${k} must be a waypoint array`);
      continue;
    }
    v.forEach((w, i) => {
      if (typeof w?.lon !== 'number' || typeof w?.lat !== 'number') {
        errors.push(`route ${k}[${i}] invalid waypoint (lon/lat required)`);
      } else if (w.lat < -90 || w.lat > 90 || w.lon < -180 || w.lon > 180) {
        errors.push(`route ${k}[${i}] out of range`);
      }
    });
  }
  return errors.length ? { ok: false, errors } : { ok: true };
}

/**
 * DB 행 배열 → 항로 객체. 행: { route_key, waypoints }
 * @returns {object} { routeKey: [waypoints] }
 */
function rowsToObject(rows) {
  const out = {};
  for (const r of rows || []) {
    if (r && r.route_key) out[r.route_key] = r.waypoints;
  }
  return out;
}

/**
 * 항로 객체 → upsert 행 배열. { routeKey: [wps] } → [{ route_key, waypoints }]
 */
function objectToRows(obj) {
  return Object.entries(obj || {}).map(([route_key, waypoints]) => ({ route_key, waypoints }));
}

module.exports = { validateRoutesPayload, rowsToObject, objectToRows };
