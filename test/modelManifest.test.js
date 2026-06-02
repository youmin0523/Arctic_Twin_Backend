// 모델 MANIFEST 순수 로직 테스트 — 스키마 검증 + 무결성 대조(diff) + 다운로드 목록.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  buildEntry,
  validateManifest,
  diffManifest,
  isVerifyClean,
  buildDownloadList,
  MANIFEST_VERSION,
} = require('../src/lib/modelManifest');

const SHA = 'a'.repeat(64);
const SHA2 = 'b'.repeat(64);

const mkManifest = (models) => ({ manifest_version: MANIFEST_VERSION, models });

test('buildEntry — 표준 항목 형태', () => {
  const e = buildEntry({ path: 'model/x.pt', size: 100, sha256: SHA, role: 'sar' });
  assert.equal(e.path, 'model/x.pt');
  assert.equal(e.size, 100);
  assert.equal(e.role, 'sar');
});

test('validateManifest — 정상 통과', () => {
  const m = mkManifest([buildEntry({ path: 'a.pt', size: 1, sha256: SHA })]);
  assert.equal(validateManifest(m).ok, true);
});

test('validateManifest — 잘못된 버전 거부', () => {
  const r = validateManifest({ manifest_version: 999, models: [] });
  assert.equal(r.ok, false);
});

test('validateManifest — sha256 길이 위반 탐지', () => {
  const r = validateManifest(mkManifest([{ path: 'a.pt', size: 1, sha256: 'short' }]));
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('sha256')));
});

test('validateManifest — 경로 중복 탐지', () => {
  const r = validateManifest(mkManifest([
    { path: 'dup.pt', size: 1, sha256: SHA },
    { path: 'dup.pt', size: 2, sha256: SHA2 },
  ]));
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('duplicate')));
});

test('diffManifest — 모두 일치하면 ok', () => {
  const m = mkManifest([{ path: 'a.pt', size: 10, sha256: SHA }]);
  const actual = { 'a.pt': { size: 10, sha256: SHA } };
  const d = diffManifest(m, actual);
  assert.deepEqual(d.ok, ['a.pt']);
  assert.equal(d.missing.length, 0);
  assert.equal(isVerifyClean(d), true);
});

test('diffManifest — 누락 파일 탐지', () => {
  const m = mkManifest([{ path: 'a.pt', size: 10, sha256: SHA }]);
  const d = diffManifest(m, {});
  assert.deepEqual(d.missing, ['a.pt']);
  assert.equal(isVerifyClean(d), false);
});

test('diffManifest — 크기/해시 불일치는 손상', () => {
  const m = mkManifest([{ path: 'a.pt', size: 10, sha256: SHA }]);
  const actual = { 'a.pt': { size: 11, sha256: SHA2 } };
  const d = diffManifest(m, actual);
  assert.equal(d.corrupt.length, 1);
  assert.equal(d.corrupt[0].path, 'a.pt');
  assert.equal(isVerifyClean(d), false);
});

test('diffManifest — manifest 에 없는 로컬 파일은 extra', () => {
  const m = mkManifest([{ path: 'a.pt', size: 10, sha256: SHA }]);
  const actual = { 'a.pt': { size: 10, sha256: SHA }, 'b.pt': { size: 5, sha256: SHA2 } };
  const d = diffManifest(m, actual);
  assert.deepEqual(d.extra, ['b.pt']);
});

test('diffManifest — Map 입력도 지원', () => {
  const m = mkManifest([{ path: 'a.pt', size: 10, sha256: SHA }]);
  const actual = new Map([['a.pt', { size: 10, sha256: SHA }]]);
  assert.equal(diffManifest(m, actual).ok.length, 1);
});

test('buildDownloadList — baseUrl 로 누락 파일 URL 생성', () => {
  const diff = { missing: ['model/x.pt'], corrupt: [], ok: [], extra: [] };
  const list = buildDownloadList(diff, 'https://cdn.example.com/models/');
  assert.equal(list[0].url, 'https://cdn.example.com/models/model/x.pt');
});

test('buildDownloadList — baseUrl 없으면 url=null', () => {
  const diff = { missing: ['model/x.pt'], corrupt: [], ok: [], extra: [] };
  assert.equal(buildDownloadList(diff, '')[0].url, null);
});
