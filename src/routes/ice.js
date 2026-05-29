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
  const archiveDir = path.join(dataDir, 'archive');
  const entries = [];

  // 1. 월별 파일 (realIceData_month01~12.json) - 헤더 300바이트에서 실제 date 추출
  for (let m = 1; m <= 12; m++) {
    const mm = String(m).padStart(2, '0');
    const filePath = path.join(dataDir, `realIceData_month${mm}.json`);
    if (!fs.existsSync(filePath)) continue;
    try {
      const fd = fs.openSync(filePath, 'r');
      const buf = Buffer.alloc(300);
      fs.readSync(fd, buf, 0, 300, 0);
      fs.closeSync(fd);
      const header = buf.toString('utf-8');
      const dateMatch = header.match(/"date"\s*:\s*"(\d{8})"/);
      if (dateMatch) {
        const d = dateMatch[1]; // "20230415"
        const value = `${d.slice(0, 4)}-${d.slice(4, 6)}`; // "2023-04"
        entries.push({ value, label: `${value} (${m}월)` });
      } else {
        entries.push({ value: `month-${mm}`, label: `${m}월` });
      }
    } catch (_) {
      entries.push({ value: `month-${mm}`, label: `${m}월` });
    }
  }

  // 2. 날짜별 아카이브 (realIceData_YYYY-MM-DD.json) - copernicus_fetcher 생성분
  if (fs.existsSync(archiveDir)) {
    const files = fs.readdirSync(archiveDir);
    const datedEntries = files
      .map(f => f.match(/^realIceData_(\d{4}-\d{2}-\d{2})\.json$/)?.[1])
      .filter(Boolean)
      .sort()
      .reverse()
      .map(d => ({ value: d, label: d }));
    entries.unshift(...datedEntries); // 날짜별이 상단 표시
  }

  res.json({ entries });
});

module.exports = router;
