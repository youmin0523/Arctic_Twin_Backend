const express = require('express');
const router = express.Router();
const { runPythonScript } = require('../services/pipelineRunner');

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

    res.json(JSON.parse(result));
  } catch (err) {
    console.error('[Routing] evaluate error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
