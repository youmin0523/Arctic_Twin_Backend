const express = require('express');
const router = express.Router();
const fs = require('fs');
const path = require('path');
const { getIceData } = require('../services/dataStore');

// GET /api/ice/concentration?month=2023-03
router.get('/concentration', async (req, res) => {
  try {
    const month = req.query.month || 'latest';
    const data = await getIceData('concentration', month);
    if (!data) {
      return res.status(404).json({ error: `Ice data not found for month: ${month}` });
    }
    res.json(data);
  } catch (err) {
    console.error('[Ice] concentration error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/ice/thickness?month=2023-03
router.get('/thickness', async (req, res) => {
  try {
    const month = req.query.month || 'latest';
    const data = await getIceData('thickness', month);
    if (!data) {
      return res.status(404).json({ error: `Thickness data not found for month: ${month}` });
    }
    res.json(data);
  } catch (err) {
    console.error('[Ice] thickness error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/ice/archives - 사용 가능한 아카이브 목록 (월별 + 날짜별)
router.get('/archives', (req, res) => {
  const dataDir = path.join(__dirname, '..', '..', 'data');
  const monthlyDir = path.join(dataDir, 'monthly');
  const dailyDir = path.join(dataDir, 'archive', 'daily');
  const entries = [];

  // 1. 월별 레퍼런스 (data/monthly/realIceData_month01~12.json, 1=1월 … 12=12월)
  for (let m = 1; m <= 12; m++) {
    const mm = String(m).padStart(2, '0');
    if (!fs.existsSync(path.join(monthlyDir, `realIceData_month${mm}.json`))) continue;
    entries.push({ value: `month-${mm}`, label: `${m}월` });
  }

  // 2. 날짜별 적산 아카이브 (archive/daily/realIceData_YYYYMMDD.json - copernicus_fetcher 생성분)
  if (fs.existsSync(dailyDir)) {
    const datedEntries = fs.readdirSync(dailyDir)
      .map(f => f.match(/^realIceData_(\d{8})\.json$/)?.[1])
      .filter(Boolean)
      .sort()
      .reverse()
      .map(d => {
        const iso = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`; // "2026-05-26"
        return { value: iso, label: iso };
      });
    entries.unshift(...datedEntries); // 날짜별이 상단 표시
  }

  res.json({ entries });
});

module.exports = router;
