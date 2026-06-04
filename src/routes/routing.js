const express = require('express');
const router = express.Router();
const { runPythonScript } = require('../services/pipelineRunner');

// Python stdout 에 경고/print 한 줄이 섞여도 JSON 결과를 안전하게 추출.
// 1) 통째로 parse 시도 → 2) 실패 시 뒤에서부터 JSON 으로 parse 되는 줄을 탐색.
function parsePythonJson(raw) {
  try { return JSON.parse(raw); } catch { /* fall through */ }
  const lines = String(raw).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (/^[[{]/.test(lines[i])) {
      try { return JSON.parse(lines[i]); } catch { /* keep scanning */ }
    }
  }
  throw new Error('파이썬 출력에서 JSON 을 찾지 못했습니다.');
}

// POST /api/route/evaluate
// body: { route: "NSR", vessel: { iceClass: "PC2", displacement: 25000, ... }, month: "2023-03" }
router.post('/evaluate', async (req, res) => {
  try {
    const { route, vessel, month } = req.body;
    if (!route || !vessel) {
      return res.status(400).json({ error: 'route and vessel are required' });
    }

    const result = await runPythonScript('arctic_master_router.py', [
      '--route', route,
      '--ice-class', vessel.iceClass || 'PC5',
      '--month', month || 'latest',
    ]);

    res.json(parsePythonJson(result));
  } catch (err) {
    console.error('[Routing] evaluate error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
