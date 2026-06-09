const express = require('express');
const router = express.Router();
const fs = require('fs');
const path = require('path');
const { getIceData } = require('../services/dataStore');

// GET /api/ice/concentration?month=2023-03[&hemisphere=south]
router.get('/concentration', async (req, res) => {
  try {
    const month = req.query.month || 'latest';
    const hemisphere = req.query.hemisphere === 'south' ? 'south' : 'north';
    const data = await getIceData('concentration', month, hemisphere);
    if (!data) {
      return res.status(404).json({ error: `Ice data not found for ${hemisphere} month: ${month}` });
    }
    res.json(data);
  } catch (err) {
    console.error('[Ice] concentration error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/ice/thickness?month=2023-03
// 주의: 현재 데이터 소스(realIceData_*.json)는 해빙 "농도"만 포함하고
// 두께(thickness) 그리드는 제공하지 않는다. 과거엔 이 엔드포인트가 농도
// 데이터를 그대로 두께인 것처럼 반환했으나(오배치), 실측 두께가 없으므로
// 명시적으로 미제공(501) 응답을 돌려준다. (프론트엔드는 이 엔드포인트를 사용하지 않음)
router.get('/thickness', (req, res) => {
  res.status(501).json({
    error: 'sea-ice thickness grid not available',
    detail: '현재 수집 데이터는 해빙 농도(concentration)만 포함합니다.',
    hint: 'use /api/ice/concentration',
  });
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
