// backend/scripts/sync_db.js
// ============================================================================
// JSON(backend/data) → Neon PostgreSQL 동기화 (schema 적용 + upsert).
//
// - backend 단독으로 동작 (database/ 폴더 의존 없음). backend/node_modules 의
//   pg, dotenv 를 사용하고 backend/.env 의 DATABASE_URL 로 접속한다.
// - 멱등(ON CONFLICT ... DO UPDATE upsert) → 반복 실행 안전.
// - index.js 가 fetcher 완료 후/서버 시작 시 자동 실행하며, 수동 실행도 가능:
//     cd backend && node scripts/sync_db.js
// ============================================================================
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

const CONN = process.env.DATABASE_URL;
if (!CONN) {
  console.error('❌ DATABASE_URL 환경변수가 없습니다. backend/.env 를 확인하세요.');
  process.exit(1);
}

const DATA = path.join(__dirname, '..', 'data');
const SCHEMA = path.join(__dirname, 'schema.sql');
const pool = new Pool({ connectionString: CONN, ssl: { rejectUnauthorized: false } });

// ---------- helpers ----------
const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const exists   = (p) => fs.existsSync(p);
const emptyToNull = (v) => (v === '' || v === undefined ? null : v);
const isoDate = (s) => { const m = s && String(s).match(/^\d{4}-\d{2}-\d{2}/); return m ? m[0] : null; };
// "MM/DD/YYYY" → "YYYY-MM-DD"
function mdyToISO(s) {
  if (!s || typeof s !== 'string') return null;
  const m = s.trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (m) return `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  return isoDate(s);
}

// ---------- loaders ----------
async function runSchema(c) {
  await c.query(fs.readFileSync(SCHEMA, 'utf8'));
  console.log('✓ schema.sql 적용');
}

async function loadIcebergs(c) {
  const f = path.join(DATA, 'copernicus_icebergs.json');
  if (!exists(f)) return console.warn('· skip icebergs (파일 없음)');
  let n = 0;
  for (const r of readJson(f).icebergs || []) {
    await c.query(
      `INSERT INTO icebergs (id,lat,lon,source,period) VALUES ($1,$2,$3,$4,$5)
       ON CONFLICT (id) DO UPDATE SET lat=EXCLUDED.lat, lon=EXCLUDED.lon,
         source=EXCLUDED.source, period=EXCLUDED.period, imported_at=now()`,
      [r.id, r.lat, r.lon, r.source ?? null, r.period ?? null]);
    n++;
  }
  console.log(`✓ icebergs: ${n}건`);
}

async function loadBergs(c) {
  const f = path.join(DATA, 'realBergData_latest.json');
  if (!exists(f)) return console.warn('· skip bergs (파일 없음)');
  const d = readJson(f);
  const src = d.source ?? null, ddate = isoDate(d.date);
  let n = 0;
  for (const r of d.bergs || []) {
    await c.query(
      `INSERT INTO bergs (id,lat,lon,length_m,width_m,type,last_update,data_source,data_date)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       ON CONFLICT (id) DO UPDATE SET lat=EXCLUDED.lat, lon=EXCLUDED.lon,
         length_m=EXCLUDED.length_m, width_m=EXCLUDED.width_m, type=EXCLUDED.type,
         last_update=EXCLUDED.last_update, data_source=EXCLUDED.data_source,
         data_date=EXCLUDED.data_date, imported_at=now()`,
      [r.id, r.lat, r.lon, r.length_m ?? null, r.width_m ?? null, r.type ?? null,
       mdyToISO(r.last_update), src, ddate]);
    n++;
  }
  console.log(`✓ bergs: ${n}건`);
}

async function loadSar(c) {
  const f = path.join(DATA, 'sar_detections_latest.json');
  if (!exists(f)) return console.warn('· skip sar_detections (파일 없음)');
  const d = readJson(f);
  const dt = d.detection_time ? new Date(d.detection_time).toISOString() : null;
  const thr = d.confidence_threshold ?? null;
  const pp = d.products_processed ?? null;   // 배치에서 처리한 SAR 영상 개수
  let n = 0;
  for (const r of d.detections || []) {
    await c.query(
      `INSERT INTO sar_detections
         (detection_id,lat,lon,length_m,width_m,type,source,confidence,last_update,detection_time,confidence_threshold,products_processed)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       ON CONFLICT (detection_time,detection_id) DO UPDATE SET
         lat=EXCLUDED.lat, lon=EXCLUDED.lon, length_m=EXCLUDED.length_m, width_m=EXCLUDED.width_m,
         type=EXCLUDED.type, source=EXCLUDED.source, confidence=EXCLUDED.confidence,
         last_update=EXCLUDED.last_update, confidence_threshold=EXCLUDED.confidence_threshold,
         products_processed=EXCLUDED.products_processed, imported_at=now()`,
      [r.id ?? null, r.lat, r.lon, r.length_m ?? null, r.width_m ?? null, r.type ?? null,
       r.source ?? null, r.confidence ?? null, mdyToISO(r.last_update), dt, thr, pp]);
    n++;
  }
  console.log(`✓ sar_detections: ${n}건`);
}

async function loadSentinel1(c) {
  const f = path.join(DATA, 'sentinel1_catalog_latest.json');
  if (!exists(f)) return console.warn('· skip sentinel1_products (파일 없음)');
  let n = 0;
  for (const r of readJson(f).products || []) {
    await c.query(
      `INSERT INTO sentinel1_products
         (id,name,sensing_start,sensing_stop,aoi,orbit_direction,polarization,file_path,file_size_mb,download_timestamp)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
       ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, sensing_start=EXCLUDED.sensing_start,
         sensing_stop=EXCLUDED.sensing_stop, aoi=EXCLUDED.aoi, orbit_direction=EXCLUDED.orbit_direction,
         polarization=EXCLUDED.polarization, file_path=EXCLUDED.file_path,
         file_size_mb=EXCLUDED.file_size_mb, download_timestamp=EXCLUDED.download_timestamp, imported_at=now()`,
      [r.id, r.name, r.sensing_start ?? null, r.sensing_stop ?? null, r.aoi ?? null,
       emptyToNull(r.orbit_direction), emptyToNull(r.polarization), r.file_path ?? null,
       r.file_size_mb ?? null, r.download_timestamp ?? null]);
    n++;
  }
  console.log(`✓ sentinel1_products: ${n}건`);
}

async function loadWeather(c) {
  const f = path.join(DATA, 'weather_api_usage.json');
  if (!exists(f)) return console.warn('· skip weather_api_usage (파일 없음)');
  const d = readJson(f);
  if (!d.date) return console.warn('· skip weather_api_usage (date 없음)');
  await c.query(
    `INSERT INTO weather_api_usage (usage_date,calls) VALUES ($1,$2)
     ON CONFLICT (usage_date) DO UPDATE SET calls=EXCLUDED.calls, updated_at=now()`,
    [isoDate(d.date), d.calls ?? 0]);
  console.log('✓ weather_api_usage: 1건');
}

async function loadSimulations(c) {
  const dir = path.join(DATA, 'simulations');
  if (!exists(dir)) return console.warn('· skip simulations (폴더 없음)');
  let n = 0;
  for (const fn of fs.readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const stem = fn.replace(/\.json$/, '');
    const m = stem.match(/([a-z]+)_month(\d+)_arc(\d+)/i);
    let payload;
    try { payload = readJson(path.join(dir, fn)); }
    catch (e) { console.warn(`  · skip ${fn}: ${e.message} (잠김/손상)`); continue; }
    await c.query(
      `INSERT INTO simulation_results (scenario,route_code,month,arc_level,source_file,payload)
       VALUES ($1,$2,$3,$4,$5,$6)
       ON CONFLICT (scenario) DO UPDATE SET route_code=EXCLUDED.route_code, month=EXCLUDED.month,
         arc_level=EXCLUDED.arc_level, source_file=EXCLUDED.source_file,
         payload=EXCLUDED.payload, imported_at=now()`,
      [stem, m ? m[1].toUpperCase() : null, m ? +m[2] : null, m ? +m[3] : null, fn, payload]);
    n++;
  }
  console.log(`✓ simulation_results: ${n}건`);
}

// ---------- run ----------
(async () => {
  const c = await pool.connect();
  try {
    await c.query('BEGIN');
    await runSchema(c);
    await loadIcebergs(c);
    await loadBergs(c);
    await loadSar(c);
    await loadSentinel1(c);
    await loadWeather(c);
    await loadSimulations(c);
    await c.query('COMMIT');
    console.log('\n✅ DB 동기화 완료');
  } catch (e) {
    await c.query('ROLLBACK');
    console.error('\n❌ 실패 — 전체 롤백:', e.message);
    process.exitCode = 1;
  } finally {
    c.release();
    await pool.end();
  }
})();
