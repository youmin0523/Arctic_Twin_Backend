const express = require('express');
const router = express.Router();
const { getIcebergData, getCopernicusIcebergData } = require('../services/dataStore');

// GET /api/icebergs/latest[?date=YYYY-MM-DD]
// NIC/IIP 빙산 + Copernicus SAR 빙산 통합 반환.
// date 지정 시(빙하 아카이브) 해당 날짜 실측 빙산 아카이브를 반환하며,
//   현재 시점 전용인 Copernicus SAR는 제외한다(과거 날짜에 최신 SAR 혼입 방지).
router.get('/latest', async (req, res) => {
  try {
    const date = /^\d{4}-\d{2}-\d{2}$/.test(req.query.date || '') ? req.query.date : null;
    const [nicData, copData] = await Promise.all([
      getIcebergData(date),
      date ? Promise.resolve(null) : getCopernicusIcebergData(),
    ]);

    // NIC/IIP 빙산 (남극 이미 필터링됨)
    const nicBergs = (nicData?.bergs || []).map(b => ({
      ...b,
      source: b.source || 'NIC/IIP',
    }));

    // Copernicus SAR 빙산
    // 주의: Copernicus 소스는 빙산 치수를 제공하지 않는다. length/width 는
    // 렌더링용 대표값(추정)이며 실측이 아니다 → size_estimated 플래그로 명시.
    const copBergs = (copData?.icebergs || []).map(b => ({
      id: b.id,
      lat: b.lat,
      lon: b.lon,
      source: b.source || 'Copernicus SAR',
      period: b.period || '',
      length_m: b.length_m ?? 3000,
      width_m: b.width_m ?? 1500,
      size_estimated: b.length_m == null,
    }));

    const allBergs = [...nicBergs, ...copBergs];

    res.json({
      source: 'NIC/IIP + Copernicus SAR',
      date: nicData?.date || new Date().toISOString().split('T')[0],
      updated_at: copData?.updated_at || nicData?.date || null,
      berg_count: allBergs.length,
      nic_count: nicBergs.length,
      copernicus_count: copBergs.length,
      bergs: allBergs,
    });
  } catch (err) {
    console.error('[Iceberg] error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
