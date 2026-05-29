const express = require('express');
const { getWeatherData } = require('../services/dataStore');

const router = express.Router();

/**
 * GET /api/weather/latest
 * 5개 항로 기상 데이터 반환 (Open-Meteo, 6시간 주기 갱신).
 * weather_fetcher.py 가 수집한 weather_latest.json 캐시를 서빙한다.
 * 브라우저는 캐시된 JSON만 받으며, Open-Meteo API를 직접 호출하지 않음.
 */
router.get('/latest', async (req, res) => {
  try {
    const data = await getWeatherData();
    if (!data) {
      return res.status(503).json({
        error: 'Weather data not yet available. Run weather_fetcher.py to populate.',
      });
    }
    // 브라우저 캐시: 10분간 재요청 방지, 30분간 만료된 캐시 허용
    res.set('Cache-Control', 'public, max-age=600, stale-while-revalidate=1800');
    if (data.fetched_at) {
      res.set('Last-Modified', new Date(data.fetched_at).toUTCString());
    }
    res.json(data);
  } catch (err) {
    console.error('[Weather] /latest error:', err.message);
    res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;
