// 편집 항로 저장 순수 로직 테스트 — 검증 + DB행↔객체 변환.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  validateRoutesPayload,
  rowsToObject,
  objectToRows,
} = require('../src/lib/editedRoutesStore');

test('validateRoutesPayload — 정상 페이로드 통과', () => {
  const r = validateRoutesPayload({
    NSR: [{ lon: 129, lat: 35, label: '부산' }, { lon: 30, lat: 75 }],
  });
  assert.equal(r.ok, true);
});

test('validateRoutesPayload — 객체 아니면 거부', () => {
  assert.equal(validateRoutesPayload(null).ok, false);
  assert.equal(validateRoutesPayload([1, 2]).ok, false);
});

test('validateRoutesPayload — 항로 값이 배열 아니면 거부', () => {
  const r = validateRoutesPayload({ NSR: 'not-array' });
  assert.equal(r.ok, false);
  assert.match(r.errors[0], /waypoint array/);
});

test('validateRoutesPayload — 잘못된 웨이포인트 거부', () => {
  const r = validateRoutesPayload({ NSR: [{ lon: 'x', lat: 1 }] });
  assert.equal(r.ok, false);
});

test('validateRoutesPayload — 좌표 범위 위반 거부', () => {
  const r = validateRoutesPayload({ NSR: [{ lon: 200, lat: 1 }] });
  assert.equal(r.ok, false);
  assert.match(r.errors[0], /out of range/);
});

test('rowsToObject — DB 행 → 항로 객체', () => {
  const obj = rowsToObject([
    { route_key: 'NSR', waypoints: [{ lon: 1, lat: 2 }] },
    { route_key: 'NWP', waypoints: [] },
  ]);
  assert.deepEqual(Object.keys(obj).sort(), ['NSR', 'NWP']);
  assert.deepEqual(obj.NSR, [{ lon: 1, lat: 2 }]);
});

test('rowsToObject — 빈/널 안전', () => {
  assert.deepEqual(rowsToObject([]), {});
  assert.deepEqual(rowsToObject(null), {});
});

test('objectToRows — 항로 객체 → upsert 행', () => {
  const rows = objectToRows({ NSR: [{ lon: 1, lat: 2 }] });
  assert.deepEqual(rows, [{ route_key: 'NSR', waypoints: [{ lon: 1, lat: 2 }] }]);
});

test('라운드트립: object → rows → object 동일', () => {
  const obj = { NSR: [{ lon: 1, lat: 2 }], TSR: [{ lon: 3, lat: 4 }] };
  const back = rowsToObject(objectToRows(obj));
  assert.deepEqual(back, obj);
});
