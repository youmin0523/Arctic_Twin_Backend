const fs = require('fs');
const path = require('path');
const { hasDb, query } = require('./db');

const DATA_DIR = path.join(__dirname, '..', '..', 'data');

// 메모리 캐시 (TTL 5분)
const cache = new Map();
const CACHE_TTL = 5 * 60 * 1000;

function getCached(key) {
  const entry = cache.get(key);
  if (entry && Date.now() - entry.timestamp < CACHE_TTL) {
    return entry.data;
  }
  cache.delete(key);
  return null;
}

function setCache(key, data) {
  cache.set(key, { data, timestamp: Date.now() });
}

async function readJsonFile(filePath) {
  try {
    const raw = await fs.promises.readFile(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

// 해빙 농도 데이터
// [의도] 해빙 농도(realIceData_*.json)는 대용량 지오공간 blob 이라 DB 미동기화 — 파일 서빙 전용.
// (DB-first 는 구조화/쿼리 가능한 데이터셋(icebergs/bergs/sentinel1/sar/simulations)에만 적용.
//  단일 EC2 운영이므로 fetcher 쓰기 ↔ reader 읽기가 같은 디스크 → 파일 서빙으로 충분)
async function getIceData(type, month) {
  const cacheKey = `ice_${type}_${month}`;
  const cached = getCached(cacheKey);
  if (cached) return cached;

  // 월별 레퍼런스는 data/monthly/, latest 라이브 데이터는 data/ 최상위
  const MONTHLY_DIR = path.join(DATA_DIR, 'monthly');

  let filePath;
  if (month === 'latest') {
    filePath = path.join(DATA_DIR, 'realIceData_latest.json');
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(month)) {
    // 날짜별 적산 아카이브: "2026-05-26" → archive/daily/realIceData_20260526.json
    filePath = path.join(DATA_DIR, 'archive', 'daily', `realIceData_${month.replace(/-/g, '')}.json`);
  } else {
    // 월별: "2023-03" 또는 "month-03" → "03"
    const mm = month.includes('-') ? month.split('-')[1] : month;
    filePath = path.join(MONTHLY_DIR, `realIceData_month${mm}.json`);
  }

  let data = await readJsonFile(filePath);

  // latest 파일이 없으면 월별 파일 중 가장 최근(12→1 순) 것으로 폴백
  if (!data && month === 'latest') {
    for (let m = 12; m >= 1; m--) {
      const mm = String(m).padStart(2, '0');
      data = await readJsonFile(path.join(MONTHLY_DIR, `realIceData_month${mm}.json`));
      if (data) { console.log(`[DataStore] realIceData_latest.json 없음 → month${mm} 폴백`); break; }
    }
  }

  if (data) setCache(cacheKey, data);
  return data;
}

// 빙산 데이터 (NIC/IIP — 남극 필터링)
// DB(bergs 테이블) 우선 조회, 실패 시 realBergData_latest.json 폴백.
async function getIcebergData() {
  const cacheKey = 'icebergs_latest';
  const cached = getCached(cacheKey);
  if (cached) return cached;

  let data = null;

  if (hasDb()) {
    try {
      // lat >= 0 (남극 제외) 필터를 SQL 로 이동. last_update 는 원본 MM/DD/YYYY 문자열로 복원.
      const { rows } = await query(
        `SELECT id, lat, lon, length_m, width_m, type,
                to_char(last_update, 'MM/DD/YYYY') AS last_update,
                data_source, to_char(data_date, 'YYYY-MM-DD') AS data_date
           FROM bergs
          WHERE lat >= 0
          ORDER BY id`
      );
      // 원본 파일 구조 복원: { source, date, berg_count, bergs:[{id,lat,lon,length_m,width_m,type,last_update}] }
      const bergs = rows.map((r) => ({
        id: r.id,
        lat: r.lat,
        lon: r.lon,
        length_m: r.length_m,
        width_m: r.width_m,
        type: r.type,
        last_update: r.last_update,
      }));
      data = {
        source: rows[0]?.data_source ?? null,
        date: rows[0]?.data_date ?? null,
        berg_count: bergs.length,
        bergs,
      };
    } catch (err) {
      console.warn('[DataStore] bergs DB 조회 실패 → 파일 폴백:', err.message);
      data = null;
    }
  }

  if (!data) {
    data = await readJsonFile(path.join(DATA_DIR, 'realBergData_latest.json'));
    if (data && data.bergs) {
      // 남극 빙산(lat < 0) 필터링
      data.bergs = data.bergs.filter(b => b.lat >= 0);
      data.berg_count = data.bergs.length;
    }
  }

  if (data) setCache(cacheKey, data);
  return data;
}

// Copernicus SAR 빙산 데이터 (파일 변경 감지로 자동 갱신)
const COP_FILE = path.join(DATA_DIR, 'copernicus_icebergs.json');
let copDataCache = null;
let copFileMtime = 0;

async function getCopernicusIcebergData() {
  // DB(icebergs 테이블) 우선 조회. TTL 캐시(copDataCache) 재사용.
  if (hasDb()) {
    const cached = getCached('copernicus_icebergs_db');
    if (cached) return cached;
    try {
      const { rows } = await query(
        `SELECT id, lat, lon, source, period FROM icebergs ORDER BY id`
      );
      const { rows: meta } = await query(
        `SELECT count(*)::int AS count, max(imported_at) AS updated_at FROM icebergs`
      );
      // 원본 파일 구조 복원: { count, updated_at, icebergs:[{id,lat,lon,source,period}] }
      const data = {
        count: meta[0]?.count ?? rows.length,
        updated_at: meta[0]?.updated_at ? new Date(meta[0].updated_at).toISOString() : null,
        icebergs: rows,
      };
      setCache('copernicus_icebergs_db', data);
      return data;
    } catch (err) {
      console.warn('[DataStore] icebergs DB 조회 실패 → 파일 폴백:', err.message);
    }
  }

  // 폴백: 파일 변경 감지로 자동 갱신
  try {
    const stat = await fs.promises.stat(COP_FILE);
    const mtime = stat.mtimeMs;
    // 파일이 변경되었거나 캐시 없으면 다시 읽기
    if (!copDataCache || mtime > copFileMtime) {
      copDataCache = await readJsonFile(COP_FILE);
      copFileMtime = mtime;
      if (copDataCache) console.log(`[DataStore] copernicus_icebergs.json reloaded (${copDataCache.count || '?'} icebergs)`);
    }
  } catch {
    // 파일 없으면 null
  }
  return copDataCache;
}

// Sentinel-1 IW 빙하 아카이브 카탈로그 (파일 변경 감지로 자동 갱신)
const S1_FILE = path.join(DATA_DIR, 'sentinel1_catalog_latest.json');
let s1DataCache = null;
let s1FileMtime = 0;

async function getSentinel1Catalog() {
  // DB(sentinel1_products 테이블) 우선 조회. TTL 캐시 재사용.
  if (hasDb()) {
    const cached = getCached('sentinel1_catalog_db');
    if (cached) return cached;
    try {
      // sentinel1.js 가 sensing_start 를 문자열 범위 비교에 사용하므로 ISO 문자열로 직렬화.
      const { rows } = await query(
        `SELECT id, name,
                to_char(sensing_start, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS sensing_start,
                to_char(sensing_stop,  'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS sensing_stop,
                aoi, orbit_direction, polarization, file_path, file_size_mb,
                to_char(download_timestamp, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS download_timestamp
           FROM sentinel1_products
          ORDER BY sensing_start DESC NULLS LAST`
      );
      // 원본 파일 구조 복원: { source, product_count, products:[...] }
      // source 는 원본 sentinel1_catalog_latest.json 의 값과 동일하게 맞춤.
      const data = {
        source: 'Copernicus Data Space Ecosystem (CDSE)',
        product_count: rows.length,
        products: rows,
      };
      setCache('sentinel1_catalog_db', data);
      return data;
    } catch (err) {
      console.warn('[DataStore] sentinel1 DB 조회 실패 → 파일 폴백:', err.message);
    }
  }

  // 폴백: 파일 변경 감지로 자동 갱신
  try {
    const stat = await fs.promises.stat(S1_FILE);
    const mtime = stat.mtimeMs;
    if (!s1DataCache || mtime > s1FileMtime) {
      s1DataCache = await readJsonFile(S1_FILE);
      s1FileMtime = mtime;
      if (s1DataCache) console.log(`[DataStore] sentinel1_catalog reloaded (${s1DataCache.product_count || '?'} products)`);
    }
  } catch {
    // 파일 아직 생성 전
  }
  return s1DataCache;
}

// 기상 데이터 (Open-Meteo — weather_fetcher.py 수집, 5개 항로)
// [의도] weather_latest.json 은 대용량 페이로드라 DB 미동기화 — 파일 서빙 전용(getIceData 동일 사유).
//   weather_api_usage 테이블은 일일 호출 예산 모니터링용 sink(직접 SQL 조회), HTTP reader 없음(정상).
// 6시간마다 갱신되므로 캐시 TTL을 30분으로 설정
const WEATHER_CACHE_TTL = 30 * 60 * 1000;

async function getWeatherData() {
  const cacheKey = 'weather_latest';
  const entry = cache.get(cacheKey);
  if (entry && Date.now() - entry.timestamp < WEATHER_CACHE_TTL) {
    return entry.data;
  }
  cache.delete(cacheKey);
  let data = await readJsonFile(path.join(DATA_DIR, 'weather_latest.json'));
  if (!data) data = await readJsonFile(path.join(DATA_DIR, 'arctic_weather_latest.json'));
  if (data) cache.set(cacheKey, { data, timestamp: Date.now() });
  return data;
}

// 모든 읽기 캐시 무효화 (DB 동기화 직후 호출 → 다음 조회가 최신 DB 반영)
function clearCache() {
  cache.clear();
  copDataCache = null; copFileMtime = 0;
  s1DataCache = null; s1FileMtime = 0;
}

module.exports = { getIceData, getIcebergData, getCopernicusIcebergData, getWeatherData, getSentinel1Catalog, clearCache };
