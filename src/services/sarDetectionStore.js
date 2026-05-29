/**
 * sarDetectionStore.js
 * ====================
 * SAR YOLOv8 빙산 탐지 결과 (sar_detections_latest.json) 리더.
 *
 * 이 파일은 SAR-RL 콜라보 전용입니다.
 * iceberg_detector.py (CLI) 가 생성하는 sar_detections_latest.json 만 다루며,
 * 기존 NIC/IIP 또는 Copernicus 빙산 데이터와는 독립적입니다.
 */

const fs = require('fs');
const path = require('path');
const { hasDb, query } = require('./db');

const SAR_FILE = path.join(__dirname, '..', '..', 'data', 'sar_detections_latest.json');

// 메모리 캐시 (TTL 60초 — SAR 갱신 주기가 길어 짧게 둘 필요 없음)
let _cache = null;
let _cacheTime = 0;
const CACHE_TTL_MS = 60 * 1000;

/**
 * SAR 탐지 결과 raw 객체 반환.
 *
 * @returns {Promise<Object|null>}
 *   {
 *     detection_time: ISO8601,
 *     products_processed: number,
 *     total_detected: number,
 *     confidence_threshold: number,
 *     detections: [{ id, lon, lat, length_m, width_m, type, source, confidence, ... }]
 *   }
 *   파일이 없거나 읽기 실패 시 null.
 */
async function getSarDetectionRaw() {
  const now = Date.now();
  if (_cache && now - _cacheTime < CACHE_TTL_MS) {
    return _cache;
  }

  // DB(sar_detections 테이블) 우선 — 최신 배치(detection_time 최대)만 조회.
  if (hasDb()) {
    try {
      const { rows } = await query(
        `SELECT detection_id, lat, lon, length_m, width_m, type, source, confidence,
                to_char(last_update, 'MM/DD/YYYY') AS last_update,
                to_char(detection_time, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS detection_time,
                confidence_threshold, products_processed
           FROM sar_detections
          WHERE detection_time = (SELECT max(detection_time) FROM sar_detections)
          ORDER BY pk`
      );
      if (rows.length > 0) {
        // 원본 파일 구조 복원.
        const parsed = {
          detection_time: rows[0].detection_time,
          products_processed: rows[0].products_processed,
          total_detected: rows.length,
          confidence_threshold: rows[0].confidence_threshold,
          detections: rows.map((r) => ({
            id: r.detection_id,
            lat: r.lat,
            lon: r.lon,
            length_m: r.length_m,
            width_m: r.width_m,
            type: r.type,
            source: r.source,
            confidence: r.confidence,
            last_update: r.last_update,
          })),
        };
        _cache = parsed;
        _cacheTime = now;
        return parsed;
      }
      // DB 에 행이 없으면 파일 폴백 시도
    } catch (err) {
      console.warn('[sarDetectionStore] DB 조회 실패 → 파일 폴백:', err.message);
    }
  }

  try {
    const raw = await fs.promises.readFile(SAR_FILE, 'utf-8');
    const parsed = JSON.parse(raw);
    _cache = parsed;
    _cacheTime = now;
    return parsed;
  } catch (err) {
    if (err.code !== 'ENOENT') {
      console.warn('[sarDetectionStore] read failed:', err.message);
    }
    return null;
  }
}

/**
 * SAR 빙산 list 만 평탄화해서 반환 (없으면 빈 배열).
 * iceberg.js 의 NIC/Copernicus 와 동일한 schema 로 정규화한다.
 *
 * @returns {Promise<Array>} [{ id, lat, lon, length_m, width_m, source, confidence }]
 */
async function getSarIcebergs() {
  const raw = await getSarDetectionRaw();
  if (!raw || !Array.isArray(raw.detections)) return [];

  return raw.detections.map((d) => ({
    id: d.id,
    lat: d.lat,
    lon: d.lon,
    length_m: d.length_m,
    width_m: d.width_m,
    type: d.type,
    source: d.source || 'sentinel1_sar',
    confidence: d.confidence,
    last_update: d.last_update,
  }));
}

/**
 * 마지막 SAR 탐지 메타 정보.
 *
 * @returns {Promise<{detection_time, total_detected, confidence_threshold, available}>}
 */
async function getSarMetadata() {
  const raw = await getSarDetectionRaw();
  if (!raw) {
    return { available: false, detection_time: null, total_detected: 0, confidence_threshold: null };
  }
  return {
    available: true,
    detection_time: raw.detection_time,
    products_processed: raw.products_processed,
    total_detected: raw.total_detected,
    confidence_threshold: raw.confidence_threshold,
  };
}

function clearSarCache() {
  _cache = null;
  _cacheTime = 0;
}

module.exports = {
  getSarDetectionRaw,
  getSarIcebergs,
  getSarMetadata,
  clearSarCache,
};
