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
