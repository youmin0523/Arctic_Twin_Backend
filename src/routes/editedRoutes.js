// editedRoutes.js — 사용자가 편집한 항로 웨이포인트를 영속 저장(전 사용자 공유).
//   GET  /api/routes/edited  → { NSR: [{lon,lat,label}], ... }
//   POST /api/routes/edited  → 본문(전체 객체)로 교체 저장
//
// 영속화: PostgreSQL(edited_routes 테이블) 우선 + data/edited_routes.json 파일 폴백.
// DB 미설정/실패 시 무중단으로 파일 경로로 동작(다른 정형 데이터와 동일 패턴).
const express = require('express');
const fs = require('fs');
const path = require('path');
const { hasDb, query } = require('../services/db');
const {
  validateRoutesPayload,
  rowsToObject,
  objectToRows,
} = require('../lib/editedRoutesStore');

const router = express.Router();
const FILE = path.join(__dirname, '..', '..', 'data', 'edited_routes.json');

function readFileRoutes() {
  try {
    return JSON.parse(fs.readFileSync(FILE, 'utf-8')) || {};
  } catch {
    return {};
  }
}

function writeFileRoutes(obj) {
  fs.writeFileSync(FILE, JSON.stringify(obj), 'utf-8');
}

// 전체 편집 항로 조회 — DB 우선, 실패 시 파일 폴백
router.get('/edited', async (req, res) => {
  if (hasDb()) {
    try {
      const { rows } = await query('SELECT route_key, waypoints FROM edited_routes');
      return res.json(rowsToObject(rows));
    } catch (err) {
      console.warn('[editedRoutes] DB 조회 실패 → 파일 폴백:', err.message);
    }
  }
  res.json(readFileRoutes());
});

// 전체 편집 항로 저장(교체) — DB 우선(트랜잭션 upsert+삭제), 파일도 미러링
router.post('/edited', async (req, res) => {
  const body = req.body;
  const v = validateRoutesPayload(body);
  if (!v.ok) {
    return res.status(400).json({ error: v.errors[0], details: v.errors });
  }

  if (hasDb()) {
    try {
      const rows = objectToRows(body);
      const keys = rows.map((r) => r.route_key);
      // 교체 시맨틱: 본문에 없는 키는 삭제, 있는 키는 upsert
      if (keys.length > 0) {
        await query(
          `DELETE FROM edited_routes WHERE route_key <> ALL($1::text[])`,
          [keys],
        );
        for (const { route_key, waypoints } of rows) {
          await query(
            `INSERT INTO edited_routes (route_key, waypoints, updated_at)
             VALUES ($1, $2::jsonb, now())
             ON CONFLICT (route_key)
             DO UPDATE SET waypoints = EXCLUDED.waypoints, updated_at = now()`,
            [route_key, JSON.stringify(waypoints)],
          );
        }
      } else {
        await query('DELETE FROM edited_routes');
      }
      // 파일도 미러(폴백 대비 일관성 유지)
      try { writeFileRoutes(body); } catch { /* 미러 실패는 무시 */ }
      return res.json({ ok: true, routes: keys.length, store: 'db' });
    } catch (err) {
      console.warn('[editedRoutes] DB 저장 실패 → 파일 폴백:', err.message);
    }
  }

  // 파일 폴백
  try {
    writeFileRoutes(body);
    res.json({ ok: true, routes: Object.keys(body).length, store: 'file' });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
