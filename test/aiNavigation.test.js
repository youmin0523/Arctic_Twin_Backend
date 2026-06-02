// AI 항해 통합 집약 로직 테스트 — 부분 가용/전체 실패 graceful degradation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { aggregateNavigation, normalizeModule } = require('../src/lib/aiNavigation');

const ok = (data) => ({ status: 'fulfilled', value: { ok: true, data } });
const fail = (msg) => ({ status: 'fulfilled', value: { ok: false, error: msg } });
const rejected = (msg) => ({ status: 'rejected', reason: new Error(msg) });

test('normalizeModule — fulfilled+ok → ok', () => {
  const m = normalizeModule('fuel', ok({ liters: 100 }));
  assert.equal(m.ok, true);
  assert.deepEqual(m.data, { liters: 100 });
});

test('normalizeModule — rejected → 실패 + 사유', () => {
  const m = normalizeModule('fuel', rejected('boom'));
  assert.equal(m.ok, false);
  assert.match(m.error, /boom/);
});

test('normalizeModule — fulfilled+ok:false → 실패', () => {
  const m = normalizeModule('rl', fail('timeout'));
  assert.equal(m.ok, false);
  assert.equal(m.error, 'timeout');
});

test('aggregateNavigation — 전 모듈 가용 → status ok', () => {
  const r = aggregateNavigation({
    avoidance: ok({ path: [] }),
    departure: ok({ date: '2026-06-10' }),
    fuel: ok({ liters: 500 }),
  });
  assert.equal(r.status, 'ok');
  assert.equal(r.available, 3);
  assert.equal(r.degraded, false);
  assert.deepEqual(r.unavailable, []);
});

test('aggregateNavigation — 일부 실패 → degraded + 부분 결과', () => {
  const r = aggregateNavigation({
    avoidance: ok({ path: [] }),
    departure: fail('8002 down'),
    fuel: rejected('ECONNREFUSED'),
  });
  assert.equal(r.status, 'degraded');
  assert.equal(r.available, 1);
  assert.equal(r.degraded, true);
  assert.deepEqual(r.unavailable.sort(), ['departure', 'fuel']);
  assert.ok(r.modules.avoidance.ok);
  assert.equal(r.modules.departure.ok, false);
});

test('aggregateNavigation — 전 모듈 실패 → unavailable', () => {
  const r = aggregateNavigation({
    avoidance: fail('x'),
    departure: fail('y'),
    fuel: fail('z'),
  });
  assert.equal(r.status, 'unavailable');
  assert.equal(r.available, 0);
  assert.match(r.summary, /없음/);
});

test('aggregateNavigation — 입력 누락도 안전 처리', () => {
  const r = aggregateNavigation({});
  assert.equal(r.status, 'unavailable');
  assert.equal(r.total, 3);
  assert.equal(r.modules.avoidance.ok, false);
});

test('aggregateNavigation — summary 에 가용 모듈 수 표기', () => {
  const r = aggregateNavigation({ avoidance: ok({}), departure: ok({}), fuel: fail('x') });
  assert.match(r.summary, /2\/3/);
});
