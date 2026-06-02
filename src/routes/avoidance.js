// avoidance.js — RL 회피 메트릭 영속화(세션 간 추세 분석용).
//   POST /api/avoidance/log      → 회피 스냅샷 1건을 JSONL 로 적재
//   GET  /api/avoidance/summary  → 누적 로그 집계 통계
const express = require('express');
const fs = require('fs');
const path = require('path');
const { normalizeSnapshot, summarize, parseJsonl } = require('../lib/avoidanceLog');

const router = express.Router();
const LOG_FILE = path.join(__dirname, '..', '..', 'data', 'rl_avoidance_log.jsonl');

router.post('/log', (req, res) => {
  const { ok, record, errors } = normalizeSnapshot(req.body, new Date().toISOString());
  if (!ok) {
    return res.status(400).json({ error: 'invalid snapshot', details: errors });
  }
  try {
    fs.appendFileSync(LOG_FILE, JSON.stringify(record) + '\n', 'utf-8');
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/summary', (req, res) => {
  try {
    const text = fs.existsSync(LOG_FILE) ? fs.readFileSync(LOG_FILE, 'utf-8') : '';
    res.json(summarize(parseJsonl(text)));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
