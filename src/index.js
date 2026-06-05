require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const express = require('express');
const cors = require('cors');
const path = require('path');
const { execFile, spawn } = require('child_process');
const schedule = require('node-schedule');
const { createProxyMiddleware, fixRequestBody } = require('http-proxy-middleware');
const swaggerUi = require('swagger-ui-express');
const openapiSpec = require('./docs/openapi');
const { uvEnv, uvCommand, VENV_PYTHON } = require('./services/uvPython');

const iceRouter = require('./routes/ice');
const icebergRouter = require('./routes/iceberg');
const routingRouter = require('./routes/routing');
const proxyRouter = require('./routes/proxy');
const { legacyNsidcProxy, legacyCopProxy, legacySentinelProxy } = require('./routes/proxy');
const pipelineRouter = require('./routes/pipeline');
const weatherRouter = require('./routes/weather');
const sentinel1Router = require('./routes/sentinel1');
// (report 라우터는 /api/report 프록시가 전담하므로 마운트하지 않음 — routes/report.js 는 미사용)
const collabRouter = require('./routes/collab'); // SAR-RL 콜라보 (신규)
const simulationsRouter = require('./routes/simulations'); // 시뮬레이션 결과 DB 서빙 (신규)
const editedRoutesRouter = require('./routes/editedRoutes'); // 사용자 편집 항로 영속(공유)
const avoidanceRouter = require('./routes/avoidance'); // RL 회피 메트릭 영속화(세션 간 추세)
const aiRouter = require('./routes/ai'); // AI 항해 통합 엔드포인트(회피+출항+연료 집약)

const app = express();
const PORT = process.env.PORT || 8000;

// 미들웨어
// CORS: 기본은 모두 허용(개발/현재 배포 호환). 운영에서 CORS_ALLOWED_ORIGINS
// (콤마 구분, 예: "https://arctictwin.com,https://www.arctictwin.com")를 설정하면
// 해당 오리진만 허용 — 와일드카드 CORS + 프록시 결합 위험을 운영에서 좁힌다.
const corsAllowed = (process.env.CORS_ALLOWED_ORIGINS || '')
  .split(',').map((s) => s.trim()).filter(Boolean);
app.use(cors(corsAllowed.length ? {
  origin(origin, cb) {
    // Origin 없음(서버-서버/curl/동일출처)·허용목록 매칭 시 통과, 그 외엔 헤더 미부여(브라우저 차단)
    cb(null, !origin || corsAllowed.includes(origin));
  },
} : undefined));
if (corsAllowed.length) {
  console.log('[Server] CORS 제한 활성:', corsAllowed.join(', '));
}
app.use(express.json({ limit: '4mb' })); // 편집 항로(다점) 저장 대비 한도 상향

// API 문서 (Swagger UI) — /api-docs 에서 인터랙티브 명세 제공.
// servers 드롭다운에서 로컬(http://localhost:8000) / 운영(https://arctictwin.com) 전환 가능.
// 원본 OpenAPI JSON 은 /api-docs.json 으로 노출(외부 도구/클라이언트 생성용).
app.get('/api-docs.json', (req, res) => res.json(openapiSpec));
app.use('/api-docs', swaggerUi.serve, swaggerUi.setup(openapiSpec, {
  explorer: true,
  customSiteTitle: 'Arctic Digital Twin — API Docs',
}));

// API 라우트
app.use('/api/ice', iceRouter);
app.use('/api/icebergs', icebergRouter);
app.use('/api/route', routingRouter);
app.use('/api/routes', editedRoutesRouter); // 사용자 편집 항로(공유 저장)
app.use('/api/pipeline', pipelineRouter);
app.use('/api/weather', weatherRouter);
app.use('/api/sentinel1', sentinel1Router);
app.use('/api/collab', collabRouter); // SAR-RL 콜라보 (신규)
app.use('/api/simulations', simulationsRouter); // 시뮬레이션 결과 DB 서빙 (신규)
app.use('/api/avoidance', avoidanceRouter); // RL 회피 메트릭 영속화 (신규)
app.use('/api/ai', aiRouter); // AI 항해 통합 엔드포인트 (신규)
app.use('/proxy', proxyRouter);

// 기존 arctic-hybrid.html 호환 프록시
app.get('/nsidc-proxy/', legacyNsidcProxy);
app.get('/cop-proxy/', legacyCopProxy);
app.get('/sentinel-proxy/', legacySentinelProxy);

// 정적 데이터 파일 서빙
app.use('/data', express.static(path.join(__dirname, '..', 'data')));

// 기존 모놀리스 HTML 서빙 (기존 방식 호환)
app.use(express.static(path.join(__dirname, '..', 'public')));

// 헬스 체크
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// 서브 서버 준비 상태 헬스 체크 (프론트엔드 폴링 게이트)
app.get('/api/health/services', async (req, res) => {
  const [rl, report, ml] = await Promise.all([
    httpHealthCheck(8001, '/api/rl/health'),
    httpHealthCheck(8002, '/api/report/health'),
    httpHealthCheck(8003, '/'),
  ]);
  res.json({ rl, report, ml });
});

// ── 공통 서버 관리 유틸 ──────────────────────────────────────
const http = require('http');

/**
 * HTTP 헬스 체크 (Promise 기반)
 * @returns {Promise<boolean>} 응답 받으면 true
 */
function httpHealthCheck(port, path = '/', timeoutMs = 5000) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path, timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

/**
 * 공통 Python 서버 관리 팩토리
 * - --reload 없음: 파일 수정이 학습 스레드를 죽이지 않도록
 * - 무제한 재시작: 지수 백오프 (3s → 6s → 12s ... 최대 60s)
 * - 헬스 체크 루프: frozen 서버 감지 후 강제 재시작
 */
function makePythonServer({ tag, port, pyArgs, cwd, healthPath = '/' }) {
  const fs = require('fs');
  let proc = null;
  let restartDelay = 3000;
  let startedAt = null;
  let healthCheckTimer = null;
  let consecutiveFailures = 0;

  function start() {
    if (!fs.existsSync(VENV_PYTHON)) {
      console.warn(`[${tag}] 공용 venv 없음: ${VENV_PYTHON} — 'uv venv backend/.venv && uv pip install -r backend/requirements.txt' 실행 필요`);
      return;
    }
    if (proc) {
      console.warn(`[${tag}] Already running (PID ${proc.pid}), skipping start`);
      return;
    }

    console.log(`[${tag}] Starting on port ${port} (uv)`);
    const { cmd, args } = uvCommand(pyArgs);
    proc = spawn(cmd, args, { cwd, env: uvEnv(), stdio: ['ignore', 'pipe', 'pipe'] });
    startedAt = Date.now();

    proc.stdout.on('data', (d) => { const m = d.toString().trim(); if (m) console.log(`[${tag}]`, m); });
    proc.stderr.on('data', (d) => { const m = d.toString().trim(); if (m) console.error(`[${tag}]`, m); });

    proc.on('close', (code) => {
      console.warn(`[${tag}] Exited (code=${code}). Restarting in ${restartDelay / 1000}s...`);
      proc = null;
      // 지수 백오프: 안정적으로 오래 실행됐으면 딜레이 리셋
      const uptime = Date.now() - (startedAt || Date.now());
      if (uptime > 60000) restartDelay = 3000;
      else restartDelay = Math.min(restartDelay * 2, 60000);
      setTimeout(start, restartDelay);
    });

    // 서버 준비 후 헬스 체크 루프 시작 (kill() 에서 취소 가능하도록 타이머 보관)
    healthCheckTimer = setTimeout(scheduleHealthCheck, 30000);
  }

  async function scheduleHealthCheck() {
    if (!proc) return; // 이미 재시작 중
    const alive = await httpHealthCheck(port, healthPath);
    if (alive) {
      consecutiveFailures = 0;
    } else {
      consecutiveFailures++;
      console.warn(`[${tag}] Health check FAILED (${consecutiveFailures}/3). Port ${port} unresponsive.`);
      if (consecutiveFailures >= 3) {
        console.error(`[${tag}] Server frozen — force killing PID ${proc?.pid}`);
        consecutiveFailures = 0;
        if (proc) {
          proc.removeAllListeners('close');
          proc.kill('SIGKILL');
          proc = null;
        }
        setTimeout(start, 3000);
        return;
      }
    }
    // 30초마다 반복
    healthCheckTimer = setTimeout(scheduleHealthCheck, 30000);
  }

  function kill() {
    if (healthCheckTimer) clearTimeout(healthCheckTimer);
    if (proc) { proc.removeAllListeners('close'); proc.kill(); proc = null; }
  }

  function getProcess() { return proc; }

  return { start, kill, getProcess };
}

// ── RL Pipeline (포트 8001) ───────────────────────────────────
const RL_PORT = 8001;

const rlServer = makePythonServer({
  tag: 'RL',
  port: RL_PORT,
  // --reload 제거: 파일 수정이 학습 스레드를 죽이지 않도록
  pyArgs: ['uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(RL_PORT)],
  cwd: path.join(__dirname, '..', 'services', 'rl-pipeline'),
  healthPath: '/api/rl/health',
});

// 하위호환: rlProcess 참조
Object.defineProperty(global, 'rlProcess', { get: () => rlServer.getProcess() });

function startRLServer() { rlServer.start(); }

// /api/rl/* → 내부 Python 서버로 프록시 (마운트 경로 보존을 위해 필터 방식으로 변경)
app.use(createProxyMiddleware('/api/rl', {
  target: `http://127.0.0.1:${RL_PORT}`,
  changeOrigin: true,
  timeout: 30000,
  proxyTimeout: 30000,
  // http-proxy-middleware v2 문법: onProxyReq/onError (v3 의 on:{} 블록은 v2에서 무시됨).
  // express.json()이 body를 먼저 소비하므로 fixRequestBody 로 재스트림 (POST 본문 누락 방지).
  onProxyReq: fixRequestBody,
  onError: (_err, _req, res) => {
    res.status(503).json({
      error: 'RL 서버에 연결할 수 없습니다.',
      fallback: true,
      detail: rlProcess ? 'RL 서버 시작 중...' : 'RL 서버가 비활성화되어 있습니다.',
    });
  },
}));

// ── Report Service (포트 8002) ────────────────────────────────
const REPORT_PORT = 8002;

const reportServer = makePythonServer({
  tag: 'Report',
  port: REPORT_PORT,
  pyArgs: ['uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(REPORT_PORT)],
  cwd: path.join(__dirname, '..', 'services', 'report-service'),
  healthPath: '/api/report/health',
});

Object.defineProperty(global, 'reportProcess', { get: () => reportServer.getProcess() });

function startReportServer() { reportServer.start(); }

// /api/report/* → 내부 Python Report 서버로 프록시 (마운트 경로 보존을 위해 필터 방식으로 변경)
app.use(createProxyMiddleware('/api/report', {
  target: `http://127.0.0.1:${REPORT_PORT}`,
  changeOrigin: true,
  timeout: 120000,
  proxyTimeout: 120000,
  // http-proxy-middleware v2 문법: onProxyReq/onError (v3 의 on:{} 블록은 v2에서 무시됨).
  // express.json()이 body를 먼저 소비하므로 fixRequestBody 로 재스트림 (POST 본문 누락 방지).
  onProxyReq: fixRequestBody,
  onError: (_err, _req, res) => {
    res.status(503).json({
      error: 'Report 서버에 연결할 수 없습니다.',
      fallback: true,
      detail: reportProcess ? 'Report 서버 시작 중...' : 'Report 서버가 비활성화되어 있습니다.',
    });
  },
}));

// ── ML Fuel Pipeline (포트 8003) ──────────────────────────────
const ML_PORT = 8003;

const mlServer = makePythonServer({
  tag: 'ML',
  port: ML_PORT,
  pyArgs: ['uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(ML_PORT)],
  cwd: path.join(__dirname, '..', 'services', 'ml-pipeline'),
  healthPath: '/',
});

Object.defineProperty(global, 'mlProcess', { get: () => mlServer.getProcess() });

function startMLServer() { mlServer.start(); }

// /api/fuel/* → 내부 Python ML 서버로 프록시
app.use(createProxyMiddleware('/api/fuel', {
  target: `http://127.0.0.1:${ML_PORT}`,
  changeOrigin: true,
  timeout: 30000,
  proxyTimeout: 30000,
  // http-proxy-middleware v2 문법: onProxyReq/onError (v3 의 on:{} 블록은 v2에서 무시됨).
  // express.json()이 body를 먼저 소비하므로 fixRequestBody 로 재스트림 (POST 본문 누락 방지).
  onProxyReq: fixRequestBody,
  onError: (_err, _req, res) => {
    res.status(503).json({
      error: 'ML 연료 예측 서버에 연결할 수 없습니다.',
      fallback: true,
      detail: mlProcess ? 'ML 서버 시작 중...' : 'ML 서버가 비활성화되어 있습니다.',
    });
  },
}));

// ── JSON → DB 동기화 ─────────────────────────────────────────────
// fetcher 들이 backend/data/*.json 을 갱신한 뒤 호출되어 DB 를 최신화한다.
// 실제 실행/동시성 제어/캐시 무효화는 services/dbSync.js 가 담당(collab.js 와 공용).
const { runMigrate } = require('./services/dbSync');

// ── Iceberg pipeline scheduler ──────────────────────────────────
const SCRIPT_PATH = path.join(__dirname, '..', 'scripts', 'update_icebergs.py');

function runIcebergPipeline() {
  console.log('[Scheduler] Running iceberg pipeline...');
  const env = uvEnv({
    COPERNICUSMARINE_SERVICE_USERNAME: process.env.COPERNICUS_MARINE_USER,
    COPERNICUSMARINE_SERVICE_PASSWORD: process.env.COPERNICUS_MARINE_PASSWORD,
  });
  const { cmd, args } = uvCommand([SCRIPT_PATH]);
  execFile(cmd, args, { env, timeout: 300000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Pipeline error:', err.message);
    if (stdout) console.log('[Scheduler]', stdout.trim());
    if (stderr) console.error('[Scheduler] stderr:', stderr.trim());
    runMigrate('icebergs'); // copernicus_icebergs.json → icebergs 테이블
  });
}

// ── NIC/NSIDC Iceberg Fetcher scheduler ─────────────────────────
const BERG_FETCHER_PATH = path.join(
  __dirname, '..', 'pipeline', 'fetchers', 'iceberg_fetcher.py'
);

function runBergFetcher() {
  console.log('[Scheduler] Running iceberg_fetcher (NIC/GitHub/NSIDC)...');
  const { cmd, args } = uvCommand([BERG_FETCHER_PATH]);
  execFile(cmd, args, { env: uvEnv(), timeout: 180000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Berg fetcher error:', err.message);
    if (stdout) console.log('[BergFetcher]', stdout.trim().slice(-500));
    if (stderr) console.error('[BergFetcher] stderr:', stderr.trim().slice(-200));
    runMigrate('bergs'); // realBergData_latest.json → bergs 테이블
  });
}

// ── Copernicus Ice Fetcher scheduler ────────────────────────────
const ICE_FETCHER_PATH = path.join(
  __dirname, '..', 'pipeline', 'fetchers', 'copernicus_fetcher.py'
);

function runIceFetcher() {
  console.log('[Scheduler] Running copernicus_fetcher (sea ice concentration)...');
  const env = uvEnv({
    COPERNICUSMARINE_SERVICE_USERNAME: process.env.COPERNICUS_MARINE_USER,
    COPERNICUSMARINE_SERVICE_PASSWORD: process.env.COPERNICUS_MARINE_PASSWORD,
  });
  const { cmd, args } = uvCommand([ICE_FETCHER_PATH]);
  execFile(cmd, args, { env, timeout: 600000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Ice fetcher error:', err.message);
    if (stdout) console.log('[IceFetcher]', stdout.trim().slice(-500));
    if (stderr) console.error('[IceFetcher] stderr:', stderr.trim().slice(-200));
  });
}

// ── Sentinel-1 IW Glacier Archive scheduler ─────────────────────
const SENTINEL1_FETCHER_PATH = path.join(
  __dirname, '..', 'pipeline', 'fetchers', 'sentinel1_iw_fetcher.py'
);

function runSentinel1Fetcher() {
  console.log('[Scheduler] Running sentinel1_iw_fetcher (glacier archive)...');
  const env = uvEnv({
    CDSE_USER: process.env.CDSE_USER,
    CDSE_PASSWORD: process.env.CDSE_PASSWORD,
  });
  const { cmd, args } = uvCommand([SENTINEL1_FETCHER_PATH]);
  execFile(cmd, args, { env, timeout: 1800000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Sentinel-1 fetcher error:', err.message);
    if (stdout) console.log('[Sentinel1]', stdout.trim().slice(-500));
    if (stderr) console.error('[Sentinel1] stderr:', stderr.trim().slice(-200));
    runMigrate('sentinel1'); // sentinel1_catalog_latest.json → sentinel1_products 테이블
  });
}

// ── Weather pipeline scheduler ───────────────────────────────────
const WEATHER_SCRIPT_PATH = path.join(
  __dirname, '..', 'pipeline', 'fetchers', 'weather_fetcher.py'
);

function runWeatherPipeline() {
  console.log('[Scheduler] Running weather pipeline (Open-Meteo, all routes)...');
  // 북극 파고/SST Copernicus 폴백이 헤드리스 서버에서 인증할 수 있도록 자격증명 주입
  // (없으면 copernicus_wave/sst_fallback 의 _copernicus_available() 가 false → 폴백 조용히 스킵)
  const env = uvEnv({
    COPERNICUSMARINE_SERVICE_USERNAME: process.env.COPERNICUS_MARINE_USER,
    COPERNICUSMARINE_SERVICE_PASSWORD: process.env.COPERNICUS_MARINE_PASSWORD,
  });
  const { cmd, args } = uvCommand([WEATHER_SCRIPT_PATH]);
  // timeout 30분: Open-Meteo + Copernicus(북극 파고/SST) 폴백이 항로별로 돌아 실측 ~22분 소요
  execFile(cmd, args, { env, timeout: 1800000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Weather pipeline error:', err.message);
    if (stdout) console.log('[Weather]', stdout.trim().slice(-500));
    if (stderr) console.error('[Weather] stderr:', stderr.trim().slice(-200));
    runMigrate('weather_api_usage'); // weather_api_usage.json → weather_api_usage 테이블
  });
}

// 매일 새벽 1시 UTC (Sentinel-1 IW 빙하 아카이브)
schedule.scheduleJob('0 1 * * *', runSentinel1Fetcher);
// 매일 새벽 2시 UTC (Copernicus 해빙 농도)
schedule.scheduleJob('0 2 * * *', runIceFetcher);
// 매일 새벽 3시 UTC (Copernicus SAR 빙산 파이프라인)
schedule.scheduleJob('0 3 * * *', runIcebergPipeline);
// 매일 새벽 4시 UTC (NIC/NSIDC 빙산 fetcher - SAR 이후)
schedule.scheduleJob('0 4 * * *', runBergFetcher);
// 6시간마다 기상 파이프라인 (Open-Meteo 전 항로)
schedule.scheduleJob('30 */6 * * *', runWeatherPipeline);

// ── 시스템 리소스 모니터 (10분마다) ──────────────────────────────
function logSystemResources() {
  const { exec } = require('child_process');
  const os = require('os');

  // CPU 사용량: Windows 는 wmic, 그 외(Linux 컨테이너 등)는 os.loadavg() 기반.
  // (wmic 는 Linux 에 없어 매번 실패 프로세스를 띄우므로 플랫폼 분기)
  function reportCpu(cb) {
    if (process.platform === 'win32') {
      exec('wmic cpu get loadpercentage /value', (err, stdout) => {
        const m = stdout && stdout.match(/LoadPercentage=(\d+)/);
        cb(m ? m[1] + '%' : 'N/A');
      });
      return;
    }
    // load1 / 코어수 → 대략적 사용률(%)
    const cores = os.cpus().length || 1;
    const pct = Math.min(100, Math.round((os.loadavg()[0] / cores) * 100));
    cb(`${pct}% (load1/${cores}c)`);
  }

  reportCpu((cpu) => {
    // GPU 사용량 (nvidia-smi 있으면, 없으면 N/A — Linux/Windows 공통)
    exec('nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits', (gpuErr, gpuStdout) => {
      const gpu = (!gpuErr && gpuStdout && gpuStdout.trim()) ? gpuStdout.trim() + '%' : 'N/A';
      console.log(`[SystemMonitor] CPU: ${cpu} | GPU: ${gpu}`);
    });
  });
}

schedule.scheduleJob('*/10 * * * *', logSystemResources);

// 서버 시작 30초 후 Copernicus SAR 빙산 파이프라인 1회 실행
setTimeout(runIcebergPipeline, 30000);
// 서버 시작 60초 후 기상 파이프라인 1회 실행
setTimeout(runWeatherPipeline, 60000);
// 서버 시작 90초 후 NIC/NSIDC berg fetcher 1회 실행
setTimeout(runBergFetcher, 90000);
// 서버 시작 120초 후 해빙 농도 fetcher 1회 실행
setTimeout(runIceFetcher, 120000);
// 서버 시작 150초 후 Sentinel-1 빙하 아카이브 1회 실행
setTimeout(runSentinel1Fetcher, 150000);
// 서버 시작 10초 후 기존 backend/data/*.json 으로 DB 1회 동기화 (부팅 직후 최신화)
setTimeout(() => runMigrate('startup'), 10000);

// 전역 에러 핸들러 — 모든 라우트/미들웨어 뒤에 등록. async 핸들러가 던진
// 에러나 next(err) 를 여기서 받아 500 JSON 으로 통일 응답(스택 노출 없음).
// 없으면 Express 기본 처리로 HTML 스택이 노출되거나 소켓이 매달릴 수 있음.
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error('[Express] 처리되지 않은 라우트 에러:', err && err.stack ? err.stack : err);
  if (res.headersSent) return next(err);
  res.status(err.status || 500).json({ error: 'internal server error' });
});

app.listen(PORT, () => {
  console.log(`[Server] Arctic Digital Twin API running on http://localhost:${PORT}`);
  console.log(`[Scheduler] Sentinel-1: 01:00 UTC | Ice: 02:00 UTC | SAR: 03:00 UTC | Berg: 04:00 UTC | Weather: every 6h`);
  // AI 서버 3종(RL/Report/ML)을 순차(stagger) 기동해 부팅 시 torch/모델 동시 로드 피크를 분산.
  // t2.medium(4GB) 같은 작은 인스턴스에서 동시 로드가 RAM 을 순간 초과해 swap 으로 빠지는 것을 막는다.
  // 큰 인스턴스에선 영향 미미(단지 ~50s 늦게 모두 기동). AI_STARTUP_STAGGER_MS=0 으로 동시 기동 복원 가능.
  const STAGGER_MS = Number(process.env.AI_STARTUP_STAGGER_MS ?? 25000);
  startRLServer();                            // t+0
  setTimeout(startReportServer, STAGGER_MS);  // t+25s (기본)
  setTimeout(startMLServer, STAGGER_MS * 2);  // t+50s (기본)
});

// 프로세스 종료 시 모든 서버 정리
const { close: closeDb } = require('./services/db');
function cleanupProcesses() {
  rlServer.kill();
  reportServer.kill();
  mlServer.kill();
  closeDb().catch(() => {});
}
process.on('exit', cleanupProcesses);
process.on('SIGINT', () => { cleanupProcesses(); process.exit(); });
process.on('SIGTERM', () => { cleanupProcesses(); process.exit(); });

// 처리되지 않은 비동기 에러로 프로세스가 조용히 죽는 것을 방지.
// 로깅만 하고 살려둔다(자동 갱신 스케줄러·AI 자식 프로세스 유지가 가용성에 중요).
process.on('unhandledRejection', (reason) => {
  console.error('[Process] unhandledRejection:', reason && reason.stack ? reason.stack : reason);
});
process.on('uncaughtException', (err) => {
  console.error('[Process] uncaughtException:', err && err.stack ? err.stack : err);
});
