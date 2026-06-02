/**
 * simulations.js
 * ==============
 * 쇄빙선 에스코트 항해 시뮬레이션 결과(simulation_results 테이블) 서빙.
 *
 * 기존에는 simulate_voyage.py 가 backend/data/simulations/*.json 으로 떨어뜨린
 * 파일을 express.static('/data') 로 직접 서빙했다. 이 라우트는 동일 데이터를
 * DB(simulation_results.payload JSONB)에서 제공한다. DB 조회 실패/미존재 시
 * data/simulations/<scenario>.json 파일로 폴백한다.
 *
 * Endpoints:
 *   GET /api/simulations              - 시나리오 목록
 *   GET /api/simulations/:scenario    - 단일 시나리오 payload (원본 파일 내용)
 */
const express = require('express');
const fs = require('fs');
const path = require('path');
const router = express.Router();
const { hasDb, query } = require('../services/db');
const { parseScenarioName } = require('../lib/scenarioMeta');

const SIM_DIR = path.join(__dirname, '..', '..', 'data', 'simulations');

// GET /api/simulations — 시나리오 목록
router.get('/', async (req, res) => {
  // DB 우선
  if (hasDb()) {
    try {
      const { rows } = await query(
        `SELECT scenario, route_code, month, arc_level, source_file
           FROM simulation_results
          ORDER BY route_code, month, arc_level`
      );
      return res.json({ source: 'db', count: rows.length, scenarios: rows });
    } catch (err) {
      console.warn('[Simulations] list DB 조회 실패 → 파일 폴백:', err.message);
    }
  }

  // 폴백: 디렉터리 스캔
  try {
    const files = fs.existsSync(SIM_DIR)
      ? fs.readdirSync(SIM_DIR).filter((f) => f.endsWith('.json'))
      : [];
    const scenarios = files.map((f) => {
      const stem = f.replace(/\.json$/, '');
      return { ...parseScenarioName(stem), source_file: f };
    });
    res.json({ source: 'file', count: scenarios.length, scenarios });
  } catch (err) {
    console.error('[Simulations] list error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/simulations/:scenario — 단일 시나리오 payload
router.get('/:scenario', async (req, res) => {
  const { scenario } = req.params;

  // DB 우선
  if (hasDb()) {
    try {
      const { rows } = await query(
        `SELECT payload FROM simulation_results WHERE scenario = $1`,
        [scenario]
      );
      if (rows.length > 0) {
        return res.json(rows[0].payload); // JSONB → 원본 파일 내용 그대로
      }
      // 행 없으면 파일 폴백 시도
    } catch (err) {
      console.warn('[Simulations] payload DB 조회 실패 → 파일 폴백:', err.message);
    }
  }

  // 폴백: 파일 읽기 (디렉터리 탈출 방지)
  const safe = path.basename(scenario);
  const fpath = path.join(SIM_DIR, `${safe}.json`);
  try {
    const raw = await fs.promises.readFile(fpath, 'utf-8');
    res.type('application/json').send(raw);
  } catch (err) {
    if (err.code === 'ENOENT') {
      return res.status(404).json({ error: `시뮬레이션 시나리오 없음: ${scenario}` });
    }
    console.error('[Simulations] payload error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
