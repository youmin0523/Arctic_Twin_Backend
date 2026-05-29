/**
 * collab.js
 * =========
 * RL-pipeline + SAR YOLOv8 콜라보 전용 라우트.
 *
 * 기존 라우트(/api/icebergs/latest 등) 를 건드리지 않고,
 * 별도 prefix(/api/collab/*) 로 SAR-RL 통합 시뮬레이션이 필요로 하는
 * 데이터·트리거를 제공한다.
 *
 * Endpoints:
 *   GET  /api/collab/sar-icebergs          - SAR YOLO 탐지 빙산만 반환
 *   GET  /api/collab/sar-metadata          - SAR 탐지 메타 (시각·신뢰도·count)
 *   GET  /api/collab/all-icebergs          - NIC + Copernicus + SAR 통합 빙산 list
 *   POST /api/collab/sar-detect-trigger    - iceberg_detector.py 서브프로세스 실행 (선택)
 */

const express = require('express');
const path = require('path');
const { spawn } = require('child_process');
const { uvEnv, uvCommand } = require('../services/uvPython');
const router = express.Router();

const { getIcebergData, getCopernicusIcebergData } = require('../services/dataStore');
const {
  getSarIcebergs,
  getSarMetadata,
  clearSarCache,
} = require('../services/sarDetectionStore');

// ── GET /api/collab/sar-icebergs ─────────────────────────────────
// SAR YOLO 탐지 결과만 반환 (sar_detections_latest.json).
router.get('/sar-icebergs', async (req, res) => {
  try {
    const [bergs, meta] = await Promise.all([getSarIcebergs(), getSarMetadata()]);
    res.json({
      source: 'sentinel1_sar (YOLOv8)',
      ...meta,
      bergs,
      berg_count: bergs.length,
    });
  } catch (err) {
    console.error('[collab/sar-icebergs] error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /api/collab/sar-metadata ─────────────────────────────────
// 마지막 SAR detection 메타만.
router.get('/sar-metadata', async (req, res) => {
  try {
    res.json(await getSarMetadata());
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── GET /api/collab/all-icebergs ─────────────────────────────────
// NIC/IIP + Copernicus + SAR YOLO 통합 빙산 list.
// 기존 /api/icebergs/latest 와 동일한 응답 구조 + sar_count 추가.
router.get('/all-icebergs', async (req, res) => {
  try {
    const [nicData, copData, sarBergs] = await Promise.all([
      getIcebergData(),
      getCopernicusIcebergData(),
      getSarIcebergs(),
    ]);

    const nicBergs = (nicData?.bergs || []).map((b) => ({
      ...b,
      source: b.source || 'NIC/IIP',
    }));

    const copBergs = (copData?.icebergs || []).map((b) => ({
      id: b.id,
      lat: b.lat,
      lon: b.lon,
      source: b.source || 'Copernicus SAR',
      period: b.period || '',
      length_m: 3000,
      width_m: 1500,
    }));

    const allBergs = [...nicBergs, ...copBergs, ...sarBergs];

    res.json({
      source: 'NIC/IIP + Copernicus SAR + Sentinel-1 YOLOv8',
      date: nicData?.date || new Date().toISOString().split('T')[0],
      updated_at: copData?.updated_at || nicData?.date || null,
      berg_count: allBergs.length,
      nic_count: nicBergs.length,
      copernicus_count: copBergs.length,
      sar_count: sarBergs.length,
      bergs: allBergs,
    });
  } catch (err) {
    console.error('[collab/all-icebergs] error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── POST /api/collab/sar-detect-trigger ──────────────────────────
// iceberg_detector.py 를 서브프로세스로 실행해 sar_detections_latest.json 갱신.
// 응답은 즉시 (백그라운드 처리), 결과는 다음 GET /sar-icebergs 호출 시 반영.
//
// body: { confidence?: number, max_products?: number }
router.post('/sar-detect-trigger', (req, res) => {
  const { confidence, max_products: maxProducts } = req.body || {};
  const script = path.resolve(
    __dirname,
    '..',
    '..',
    '..',
    'backend',
    'pipeline',
    'processors',
    'iceberg_detector.py'
  );

  const args = [script, '--latest'];
  if (typeof confidence === 'number') {
    args.push('--confidence', String(confidence));
  }
  if (typeof maxProducts === 'number') {
    args.push('--max-products', String(maxProducts));
  }

  let proc;
  try {
    const { cmd, args: uvArgs } = uvCommand(args);
    proc = spawn(cmd, uvArgs, {
      detached: true,
      stdio: 'ignore',
      env: uvEnv(),
    });
    proc.unref();
  } catch (err) {
    return res.status(500).json({ error: `spawn failed: ${err.message}` });
  }

  clearSarCache(); // 다음 GET 시 갱신된 파일 강제 리로드
  res.status(202).json({
    message: 'SAR detection triggered (background)',
    pid: proc.pid,
    args: args.slice(1),
    note: 'Result will appear in /api/collab/sar-icebergs once detection finishes.',
  });
});

module.exports = router;
