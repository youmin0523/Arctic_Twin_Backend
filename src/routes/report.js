/**
 * report.js — 동향보고서 API 라우트
 *
 * report-service (FastAPI :8002)로의 프록시는 index.js에서 처리.
 * 이 파일은 report-service 관련 추가 미들웨어/fallback 용도.
 */
const express = require('express');
const router = express.Router();

// 헬스 체크 (report-service 프록시 전 빠른 응답)
router.get('/health', (req, res) => {
  res.json({ status: 'ok', service: 'report-proxy', timestamp: new Date().toISOString() });
});

module.exports = router;
