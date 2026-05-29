const express = require('express');
const router = express.Router();
const { getSentinel1Catalog } = require('../services/dataStore');

// GET /api/sentinel1/catalog
// 빙하 아카이브 전체 카탈로그 조회
router.get('/catalog', async (req, res) => {
  try {
    const catalog = await getSentinel1Catalog();
    if (!catalog) {
      return res.status(404).json({
        error: 'Sentinel-1 카탈로그가 아직 생성되지 않았습니다. 파이프라인을 먼저 실행하세요.',
      });
    }

    res.set('Cache-Control', 'public, max-age=600, stale-while-revalidate=1800');
    res.json(catalog);
  } catch (err) {
    console.error('[Sentinel1] catalog error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sentinel1/products?aoi=svalbard&from=2026-03-01&to=2026-04-08
// 조건별 제품 필터링 조회
router.get('/products', async (req, res) => {
  try {
    const catalog = await getSentinel1Catalog();
    if (!catalog) {
      return res.status(404).json({ error: 'Sentinel-1 카탈로그 없음' });
    }

    let products = catalog.products || [];

    // AOI 필터
    const { aoi, from, to } = req.query;
    if (aoi) {
      products = products.filter(p => p.aoi === aoi);
    }

    // 날짜 범위 필터
    if (from) {
      products = products.filter(p => p.sensing_start >= from);
    }
    if (to) {
      products = products.filter(p => p.sensing_start <= to + 'T23:59:59Z');
    }

    res.json({
      source: catalog.source,
      filter: { aoi: aoi || 'all', from: from || null, to: to || null },
      product_count: products.length,
      products,
    });
  } catch (err) {
    console.error('[Sentinel1] products error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
