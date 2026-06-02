#!/usr/bin/env node
/**
 * models.js — AI 모델 MANIFEST 생성·검증·다운로드 CLI.
 *
 * 모델 파일(~591MB)을 git 에 직접 넣지 않고, MANIFEST.json(경로·크기·sha256·역할)을
 * 단일 진실원본으로 버전관리한다. 재현성과 무결성을 보장하고 SAR(YOLOv8) 언블록.
 *
 *   node scripts/models.js generate   # model/ + pipeline/models 스캔 → MANIFEST.json 갱신
 *   node scripts/models.js verify     # 로컬 파일 ↔ MANIFEST 무결성 대조 (CI/배포용, 실패 시 exit 1)
 *   node scripts/models.js download   # MODEL_BASE_URL 에서 누락 모델 받기
 *
 * 환경변수:
 *   MODEL_BASE_URL  다운로드 기준 URL (예: https://<bucket>/arctic-models)
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const {
  MANIFEST_VERSION,
  buildEntry,
  validateManifest,
  diffManifest,
  formatVerifyReport,
  isVerifyClean,
  buildDownloadList,
} = require('../src/lib/modelManifest');

const BACKEND = path.join(__dirname, '..');
const MANIFEST_PATH = path.join(BACKEND, 'model', 'MANIFEST.json');

// 스캔 대상 디렉터리 + 모델 확장자
const SCAN_DIRS = [path.join(BACKEND, 'model'), path.join(BACKEND, 'pipeline', 'models')];
const MODEL_EXT = new Set(['.pt', '.zip', '.onnx', '.pkl']);

// 역할 추론(경로 기반)
function roleOf(rel) {
  const p = rel.toLowerCase();
  if (p.includes('yolo') || p.endsWith('best.pt') || p.endsWith('last.pt')) return 'sar-yolov8';
  if (p.includes('avoidance') || p.includes('sac_') || p.includes('iceberg')) return 'rl-avoidance';
  if (p.includes('departure')) return 'rl-departure';
  if (p.includes('fuel') || p.endsWith('.pkl')) return 'fuel-xgboost';
  return 'other';
}

function sha256File(abs) {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(abs));
  return h.digest('hex');
}

function walk(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const st = fs.statSync(abs);
    if (st.isDirectory()) out.push(...walk(abs));
    else if (MODEL_EXT.has(path.extname(name).toLowerCase())) out.push(abs);
  }
  return out;
}

// 모든 모델 파일을 { relPath → {size, sha256} } 로 스캔 (relPath 는 backend/ 기준 POSIX)
function scanLocal() {
  const map = {};
  for (const dir of SCAN_DIRS) {
    for (const abs of walk(dir)) {
      const rel = path.relative(BACKEND, abs).split(path.sep).join('/');
      const st = fs.statSync(abs);
      map[rel] = { size: st.size, sha256: sha256File(abs) };
    }
  }
  return map;
}

function loadManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    console.error(`MANIFEST 없음: ${MANIFEST_PATH} — 먼저 'generate' 실행`);
    process.exit(2);
  }
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf-8'));
}

function cmdGenerate() {
  const local = scanLocal();
  const models = Object.entries(local)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([rel, info]) => buildEntry({ path: rel, size: info.size, sha256: info.sha256, role: roleOf(rel) }));
  const totalBytes = models.reduce((a, m) => a + m.size, 0);
  const manifest = {
    manifest_version: MANIFEST_VERSION,
    generated_note: 'scripts/models.js generate 로 재생성 가능. 모델 교체 시 갱신 후 커밋.',
    model_count: models.length,
    total_bytes: totalBytes,
    models,
  };
  const v = validateManifest(manifest);
  if (!v.ok) {
    console.error('생성된 MANIFEST 가 유효하지 않음:', v.errors);
    process.exit(1);
  }
  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + '\n', 'utf-8');
  console.log(`✓ MANIFEST 생성: ${models.length}개 모델, ${(totalBytes / 1e6).toFixed(1)}MB → ${path.relative(BACKEND, MANIFEST_PATH)}`);
}

function cmdVerify() {
  const manifest = loadManifest();
  const v = validateManifest(manifest);
  if (!v.ok) {
    console.error('MANIFEST 스키마 오류:', v.errors);
    process.exit(1);
  }
  const local = scanLocal();
  const diff = diffManifest(manifest, local);
  console.log(formatVerifyReport(diff));
  if (!isVerifyClean(diff)) {
    console.error('\n❌ 모델 무결성 검증 실패 — download 로 누락분을 받거나 generate 로 갱신하세요.');
    process.exit(1);
  }
  console.log('\n✅ 모든 모델이 MANIFEST 와 일치합니다.');
}

async function cmdDownload() {
  const manifest = loadManifest();
  const local = scanLocal();
  const diff = diffManifest(manifest, local);
  const base = process.env.MODEL_BASE_URL;
  const list = buildDownloadList(diff, base);
  if (list.length === 0) {
    console.log('✓ 누락 모델 없음 — 다운로드 불필요.');
    return;
  }
  if (!base) {
    console.error(`누락 ${list.length}개. MODEL_BASE_URL 환경변수를 설정하세요(예: https://<bucket>/arctic-models).`);
    list.forEach((x) => console.error(`  - ${x.path}`));
    process.exit(1);
  }
  for (const { path: rel, url } of list) {
    const dest = path.join(BACKEND, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    process.stdout.write(`↓ ${rel} ... `);
    const res = await fetch(url);
    if (!res.ok) {
      console.error(`실패 (${res.status})`);
      process.exit(1);
    }
    const buf = Buffer.from(await res.arrayBuffer());
    fs.writeFileSync(dest, buf);
    console.log(`완료 (${(buf.length / 1e6).toFixed(1)}MB)`);
  }
  console.log('재검증을 위해 verify 를 실행하세요.');
}

const cmd = process.argv[2];
({
  generate: cmdGenerate,
  verify: cmdVerify,
  download: cmdDownload,
}[cmd] || (() => {
  console.log('사용법: node scripts/models.js <generate|verify|download>');
  process.exit(cmd ? 1 : 0);
}))();
