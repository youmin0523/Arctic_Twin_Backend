// RL 회피 로그 영속화 순수 로직 테스트 — 스냅샷 검증/정규화 + 집계.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { normalizeSnapshot, summarize, parseJsonl } = require('../src/lib/avoidanceLog');

const TS = '2026-06-02T00:00:00.000Z';

test('normalizeSnapshot — 정상 스냅샷을 레코드로 정규화', () => {
  const { ok, record } = normalizeSnapshot(
    { checks: 10, threats: 4, applied: 3, rlSuccessRate: 0.75, fallbackRate: 0.25, avgConfidence: 0.6, minTcpaHours: 2.5, route: 'NSR', iceClass: 'PC5' },
    TS,
  );
  assert.equal(ok, true);
  assert.equal(record.ts, TS);
  assert.equal(record.threats, 4);
  assert.equal(record.rlSuccessRate, 0.75);
  assert.equal(record.minTcpaHours, 2.5);
  assert.equal(record.route, 'NSR');
});

test('normalizeSnapshot — 객체 아님 거부', () => {
  assert.equal(normalizeSnapshot(null, TS).ok, false);
  assert.equal(normalizeSnapshot([1, 2], TS).ok, false);
});

test('normalizeSnapshot — 필수 숫자 누락 거부', () => {
  const r = normalizeSnapshot({ threats: 'x' }, TS);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('threats')));
});

test('normalizeSnapshot — 비율 범위 위반 거부', () => {
  const r = normalizeSnapshot({ checks: 1, threats: 1, rlSuccessRate: 1.5 }, TS);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('rlSuccessRate')));
});

test('normalizeSnapshot — TCPA 없으면 null', () => {
  const { record } = normalizeSnapshot({ checks: 1, threats: 1 }, TS);
  assert.equal(record.minTcpaHours, null);
});

test('summarize — 여러 세션을 추세로 집계', () => {
  const recs = [
    { threats: 4, applied: 3, rlSuccessRate: 0.8, fallbackRate: 0.2, avgConfidence: 0.6, minTcpaHours: 3 },
    { threats: 2, applied: 2, rlSuccessRate: 0.6, fallbackRate: 0.4, avgConfidence: 0.5, minTcpaHours: 1 },
  ];
  const s = summarize(recs);
  assert.equal(s.sessions, 2);
  assert.equal(s.total_threats, 6);
  assert.equal(s.total_applied, 5);
  assert.equal(s.avg_rl_success_rate, 0.7);
  assert.equal(s.min_tcpa_hours, 1); // 최소 TCPA
});

test('summarize — 빈 입력 방어', () => {
  const s = summarize([]);
  assert.equal(s.sessions, 0);
  assert.equal(s.avg_rl_success_rate, null);
});

test('parseJsonl — 깨진 줄은 건너뛴다', () => {
  const text = '{"threats":1}\n망가진줄\n{"threats":2}\n';
  const recs = parseJsonl(text);
  assert.equal(recs.length, 2);
  assert.equal(recs[1].threats, 2);
});

test('정규화→직렬화→파싱 라운드트립', () => {
  const { record } = normalizeSnapshot({ checks: 5, threats: 2, rlSuccessRate: 0.5 }, TS);
  const line = JSON.stringify(record);
  const back = parseJsonl(line + '\n');
  assert.deepEqual(back[0], record);
});
