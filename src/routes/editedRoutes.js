// editedRoutes.js — 사용자가 편집한 항로 웨이포인트를 서버에 영속 저장(전 사용자 공유)
//   GET  /api/routes/edited  → { NSR: [{lon,lat,label}], ... }
//   POST /api/routes/edited  → 본문(전체 객체)로 교체 저장
const express = require('express');
const fs = require('fs');
const path = require('path');

const router = express.Router();
const FILE = path.join(__dirname, '..', '..', 'data', 'edited_routes.json');

function readEdited() {
  try {
    return JSON.parse(fs.readFileSync(FILE, 'utf-8')) || {};
  } catch {
    return {};
  }
}

function writeEdited(obj) {
  fs.writeFileSync(FILE, JSON.stringify(obj), 'utf-8');
}

// 전체 편집 항로 조회
router.get('/edited', (req, res) => {
  res.json(readEdited());
});

// 전체 편집 항로 저장(교체) — 본문은 { routeKey: [{lon,lat,label?}, ...], ... }
router.post('/edited', (req, res) => {
  const body = req.body;
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return res.status(400).json({ error: 'payload must be an object keyed by route' });
  }
  for (const [k, v] of Object.entries(body)) {
    if (!Array.isArray(v)) {
      return res.status(400).json({ error: `route ${k} must be a waypoint array` });
    }
    for (const w of v) {
      if (typeof w?.lon !== 'number' || typeof w?.lat !== 'number') {
        return res.status(400).json({ error: `route ${k} has invalid waypoint` });
      }
    }
  }
  try {
    writeEdited(body);
    res.json({ ok: true, routes: Object.keys(body).length });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
