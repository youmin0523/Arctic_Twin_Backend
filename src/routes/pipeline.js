const express = require('express');
const router = express.Router();
const { runPythonScript } = require('../services/pipelineRunner');

// POST /api/pipeline/run
// body: { task: "fetch_ice" | "fetch_icebergs" | "all" }
router.post('/run', async (req, res) => {
  try {
    const { task } = req.body;
    const scriptName = task === 'all' ? 'run_pipeline.py' :
                       task === 'fetch_ice' ? 'fetchers/nsidc_pipeline.py' :
                       task === 'fetch_icebergs' ? 'fetchers/iceberg_fetcher.py' :
                       task === 'fetch_sentinel1' ? 'fetchers/sentinel1_iw_fetcher.py' :
                       'run_pipeline.py';

    const result = await runPythonScript(scriptName, []);
    res.json({ status: 'completed', output: result });
  } catch (err) {
    console.error('[Pipeline] run error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
