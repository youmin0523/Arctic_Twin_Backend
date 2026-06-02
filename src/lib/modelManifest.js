/**
 * modelManifest.js
 *
 * AI 모델 파일의 MANIFEST(목록·버전·무결성) 순수 로직.
 *
 * 문제: 학습된 모델(YOLOv8 SAR 탐지기, SAC 회피/출항, XGBoost 연료 — 총 ~591MB)이
 * git LFS 로 추적되지 않아 클론 시 "미포함"되고, 버전·체크섬 기록이 없어 재현성이
 * 보장되지 않는다(발표자료 한계 #1).
 *
 * 해법: 각 모델의 경로·크기·sha256·역할을 담은 MANIFEST.json 을 단일 진실원본으로
 * 두고, 이 순수 함수들로 (a) 로컬 파일과의 무결성 대조(verify) (b) 누락 식별(download
 * 대상) 을 수행한다. 파일 I/O·해시 계산은 호출자(scripts/models.js)가 담당해
 * 이 모듈은 테스트 가능한 순수 로직으로 유지한다.
 */

const MANIFEST_VERSION = 1;

/** MANIFEST 한 항목 생성 */
function buildEntry({ path, size, sha256, role = null, version = null }) {
  return { path, size, sha256, role, version };
}

/** MANIFEST 객체 스키마 검증 */
function validateManifest(obj) {
  const errors = [];
  if (!obj || typeof obj !== 'object') return { ok: false, errors: ['manifest must be an object'] };
  if (obj.manifest_version !== MANIFEST_VERSION) {
    errors.push(`manifest_version must be ${MANIFEST_VERSION}`);
  }
  if (!Array.isArray(obj.models)) {
    errors.push('models must be an array');
    return { ok: false, errors };
  }
  obj.models.forEach((m, i) => {
    if (!m || typeof m.path !== 'string') errors.push(`models[${i}].path missing`);
    if (!Number.isInteger(m?.size) || m.size < 0) errors.push(`models[${i}].size invalid`);
    if (typeof m?.sha256 !== 'string' || m.sha256.length !== 64) errors.push(`models[${i}].sha256 invalid`);
  });
  // 경로 중복 검사
  const seen = new Set();
  for (const m of obj.models) {
    if (m && seen.has(m.path)) errors.push(`duplicate path: ${m.path}`);
    if (m) seen.add(m.path);
  }
  return { ok: errors.length === 0, errors };
}

/**
 * MANIFEST 와 실제 로컬 파일 상태를 대조.
 * @param {object} manifest - { models: [{path,size,sha256}] }
 * @param {Map<string,{size:number,sha256:string}>|object} actual - 경로→실제 파일 정보.
 *        값이 없으면(undefined) 누락으로 간주.
 * @returns {{ok: string[], missing: string[], corrupt: object[], extra: string[]}}
 */
function diffManifest(manifest, actual) {
  const get = actual instanceof Map ? (k) => actual.get(k) : (k) => actual?.[k];
  const keys = actual instanceof Map ? [...actual.keys()] : Object.keys(actual || {});
  const ok = [];
  const missing = [];
  const corrupt = [];

  const manifestPaths = new Set();
  for (const m of manifest?.models || []) {
    manifestPaths.add(m.path);
    const a = get(m.path);
    if (!a) {
      missing.push(m.path);
    } else if (a.size !== m.size || a.sha256 !== m.sha256) {
      corrupt.push({
        path: m.path,
        expected: { size: m.size, sha256: m.sha256 },
        actual: { size: a.size, sha256: a.sha256 },
      });
    } else {
      ok.push(m.path);
    }
  }
  // manifest 에 없는 로컬 파일(추적되지 않는 모델)
  const extra = keys.filter((k) => !manifestPaths.has(k));
  return { ok, missing, corrupt, extra };
}

/** diff 결과를 사람이 읽을 요약 문자열로 */
function formatVerifyReport(diff) {
  const lines = [
    `✓ OK: ${diff.ok.length}`,
    `✗ 누락(missing): ${diff.missing.length}`,
    `⚠ 손상(corrupt): ${diff.corrupt.length}`,
    `? 미추적(extra): ${diff.extra.length}`,
  ];
  for (const p of diff.missing) lines.push(`  - 누락: ${p}`);
  for (const c of diff.corrupt) lines.push(`  - 손상: ${c.path} (크기 ${c.actual.size}≠${c.expected.size})`);
  return lines.join('\n');
}

/** verify 통과 여부 (누락·손상 없으면 통과) */
function isVerifyClean(diff) {
  return diff.missing.length === 0 && diff.corrupt.length === 0;
}

/** 누락 파일의 다운로드 URL 목록 생성 */
function buildDownloadList(diff, baseUrl) {
  const base = (baseUrl || '').replace(/\/+$/, '');
  return diff.missing.map((p) => ({ path: p, url: base ? `${base}/${p}` : null }));
}

module.exports = {
  MANIFEST_VERSION,
  buildEntry,
  validateManifest,
  diffManifest,
  formatVerifyReport,
  isVerifyClean,
  buildDownloadList,
};
