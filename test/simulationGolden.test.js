// 시뮬레이션 골든 회귀 테스트 — simulate_voyage.py 가 생성한 36개 결정적
// 결과물(simulation_results)의 물리·논리 불변식을 잠근다. 시뮬레이터를 재실행하거나
// 데이터를 재생성했을 때 결과가 비물리적으로 드리프트하면 즉시 실패한다.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const SIM_DIR = path.join(__dirname, '..', 'data', 'simulations');

function loadAll() {
  if (!fs.existsSync(SIM_DIR)) return [];
  return fs
    .readdirSync(SIM_DIR)
    .filter((f) => f.endsWith('.json'))
    .map((f) => ({ file: f, data: JSON.parse(fs.readFileSync(path.join(SIM_DIR, f), 'utf-8')) }));
}

const ALL = loadAll();

test('baseline 시뮬레이션 파일이 존재', () => {
  assert.ok(ALL.length > 0, 'data/simulations/*.json 이 최소 1개 필요');
});

test('metadata.total_ticks 가 실제 ticks 길이와 일치', () => {
  for (const { file, data } of ALL) {
    assert.equal(
      data.metadata.total_ticks,
      data.ticks.length,
      `${file}: total_ticks(${data.metadata.total_ticks}) ≠ ticks.length(${data.ticks.length})`,
    );
  }
});

test('tick 시간(t)이 dt_hours 간격으로 단조 증가', () => {
  for (const { file, data } of ALL) {
    const dt = data.metadata.dt_hours;
    for (let i = 1; i < data.ticks.length; i++) {
      const delta = data.ticks[i].t - data.ticks[i - 1].t;
      assert.equal(delta, dt, `${file}: tick ${i} 시간 간격 ${delta} ≠ dt ${dt}`);
    }
  }
});

test('선박 진행거리(km_along_route)가 비감소 + 음수 아님', () => {
  for (const { file, data } of ALL) {
    let prev = -1;
    for (let i = 0; i < data.ticks.length; i++) {
      const km = data.ticks[i].ship.km_along_route;
      assert.ok(km >= 0, `${file}: tick ${i} km_along_route 음수(${km})`);
      assert.ok(km >= prev - 1e-6, `${file}: tick ${i} 진행거리 역행 (${km} < ${prev})`);
      prev = km;
    }
  }
});

test('완료된 항해는 총거리 근처에 도달', () => {
  for (const { file, data } of ALL) {
    if (!data.summary.completed) continue;
    const last = data.ticks[data.ticks.length - 1].ship.km_along_route;
    const total = data.summary.total_route_km;
    assert.ok(total > 0, `${file}: total_route_km 가 0 이하`);
    // 마지막 진행거리가 전체의 95% 이상이어야 "완료"로 일관
    assert.ok(
      last >= total * 0.95,
      `${file}: 완료로 표기됐으나 진행 ${last.toFixed(0)}/${total.toFixed(0)}km`,
    );
  }
});

test('모든 tick 의 위경도가 유효 범위', () => {
  for (const { file, data } of ALL) {
    for (let i = 0; i < data.ticks.length; i++) {
      const { lat, lon } = data.ticks[i].ship.position;
      assert.ok(lat >= -90 && lat <= 90, `${file}: tick ${i} lat 범위 위반(${lat})`);
      assert.ok(lon >= -180 && lon <= 180, `${file}: tick ${i} lon 범위 위반(${lon})`);
    }
  }
});

test('RIO 값이 유한 + POLARIS 합리적 범위(-50~10)', () => {
  for (const { file, data } of ALL) {
    for (let i = 0; i < data.ticks.length; i++) {
      const rio = data.ticks[i].ship.rio;
      assert.ok(Number.isFinite(rio), `${file}: tick ${i} RIO 비유한(${rio})`);
      assert.ok(rio >= -50 && rio <= 10, `${file}: tick ${i} RIO 범위 이탈(${rio})`);
    }
  }
});

test('summary 카운터가 음수 아님', () => {
  for (const { file, data } of ALL) {
    for (const key of ['icebreaker_calls', 'intercept_failed', 'total_escort_distance_km', 'max_rio_violation']) {
      const v = data.summary[key];
      if (v != null) assert.ok(v >= 0, `${file}: summary.${key} 음수(${v})`);
    }
  }
});
