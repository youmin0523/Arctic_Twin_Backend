// ai.js — AI 항해 통합 엔드포인트.
//   POST /api/ai/navigation
//     본문: { avoidance?: {...}, departure?: {...}, fuel?: {...} }
//   분리된 3개 AI 서비스(회피 8001 · 출항 8002 · 연료 8003)를 동시 호출해
//   하나의 일관된 의사결정으로 집약한다. 일부 서비스 미기동 시 graceful degradation.
const express = require('express');
const { aggregateNavigation } = require('../lib/aiNavigation');

const router = express.Router();

const TARGETS = {
  avoidance: { url: 'http://127.0.0.1:8001/api/rl/infer' },
  departure: { url: 'http://127.0.0.1:8002/api/report/departure/recommend' },
  fuel: { url: 'http://127.0.0.1:8003/api/fuel/predict' },
};
const TIMEOUT_MS = 8000;

async function callService(url, body) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    return { ok: true, data: await res.json() };
  } catch (e) {
    return { ok: false, error: e.name === 'AbortError' ? 'timeout' : e.message };
  } finally {
    clearTimeout(t);
  }
}

router.post('/navigation', async (req, res) => {
  const body = req.body || {};
  const [avoidance, departure, fuel] = await Promise.allSettled([
    callService(TARGETS.avoidance.url, body.avoidance),
    callService(TARGETS.departure.url, body.departure),
    callService(TARGETS.fuel.url, body.fuel),
  ]);

  const result = aggregateNavigation({ avoidance, departure, fuel });
  // 전 모듈 불가 시 503, 그 외엔 200(부분 결과 허용)
  res.status(result.status === 'unavailable' ? 503 : 200).json(result);
});

module.exports = router;
