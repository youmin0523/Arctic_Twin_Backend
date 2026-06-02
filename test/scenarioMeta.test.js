// 백엔드 계약 테스트 — 시나리오 파일명 파싱 + 시뮬레이션 payload 스키마.
// node:test (Node 내장, 무의존성)으로 실행: `npm test`
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const {
  parseScenarioName,
  validateSimulationPayload,
} = require('../src/lib/scenarioMeta');

test('parseScenarioName — 표준 파일명을 메타데이터로 분해', () => {
  const m = parseScenarioName('nsr_month01_arc4');
  assert.equal(m.route_code, 'NSR');
  assert.equal(m.month, 1);
  assert.equal(m.arc_level, 4);
  assert.equal(m.scenario, 'nsr_month01_arc4');
});

test('parseScenarioName — 두 자리 월/높은 arc 처리', () => {
  const m = parseScenarioName('nwp_month12_arc9');
  assert.equal(m.route_code, 'NWP');
  assert.equal(m.month, 12);
  assert.equal(m.arc_level, 9);
});

test('parseScenarioName — 규칙 불일치 시 null 필드', () => {
  const m = parseScenarioName('garbage-name');
  assert.equal(m.route_code, null);
  assert.equal(m.month, null);
  assert.equal(m.arc_level, null);
  assert.equal(m.scenario, 'garbage-name');
});

test('parseScenarioName — 비문자열 입력에도 throw 하지 않음', () => {
  assert.doesNotThrow(() => parseScenarioName(undefined));
  assert.equal(parseScenarioName(null).route_code, null);
});

test('validateSimulationPayload — 정상 payload 통과', () => {
  const good = {
    metadata: { route: 'NSR' },
    ticks: [{ ship: { position: { lat: 35, lon: 129 } } }],
    summary: { completed: true },
  };
  const r = validateSimulationPayload(good);
  assert.equal(r.ok, true);
  assert.deepEqual(r.errors, []);
});

test('validateSimulationPayload — 필수 키 누락 탐지', () => {
  const r = validateSimulationPayload({ metadata: { route: 'NSR' } });
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('ticks')));
  assert.ok(r.errors.some((e) => e.includes('summary')));
});

test('validateSimulationPayload — 객체 아님 방어', () => {
  assert.equal(validateSimulationPayload(null).ok, false);
  assert.equal(validateSimulationPayload('문자열').ok, false);
});

// 실제 데이터 파일이 계약(스키마)을 만족하는지 — 회귀 방지
test('실제 simulations/*.json 파일이 스키마 + 파일명 규칙을 만족', () => {
  const dir = path.join(__dirname, '..', 'data', 'simulations');
  if (!fs.existsSync(dir)) {
    console.warn('  (data/simulations 없음 — 데이터 계약 검증 건너뜀)');
    return;
  }
  const files = fs.readdirSync(dir).filter((f) => f.endsWith('.json'));
  assert.ok(files.length > 0, '시뮬레이션 파일이 최소 1개는 있어야 함');

  for (const f of files) {
    const stem = f.replace(/\.json$/, '');
    const meta = parseScenarioName(stem);
    assert.notEqual(meta.route_code, null, `파일명 규칙 위반: ${f}`);

    const payload = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8'));
    const v = validateSimulationPayload(payload);
    assert.ok(v.ok, `스키마 위반 ${f}: ${v.errors.join('; ')}`);
  }
});
