require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const express = require('express');
const cors = require('cors');
const path = require('path');
const { execFile, spawn } = require('child_process');
const schedule = require('node-schedule');
const { createProxyMiddleware } = require('http-proxy-middleware');
const { uvEnv, uvCommand, VENV_PYTHON } = require('./services/uvPython');

const iceRouter = require('./routes/ice');
const icebergRouter = require('./routes/iceberg');
const routingRouter = require('./routes/routing');
const proxyRouter = require('./routes/proxy');
const { legacyNsidcProxy, legacyCopProxy, legacySentinelProxy } = require('./routes/proxy');
const pipelineRouter = require('./routes/pipeline');
const weatherRouter = require('./routes/weather');
const sentinel1Router = require('./routes/sentinel1');
const reportRouter = require('./routes/report');
const collabRouter = require('./routes/collab'); // SAR-RL 콜라보 (신규)

const app = express();
const PORT = process.env.PORT || 8000;

// 미들웨어
app.use(cors());
app.use(express.json());

// API 라우트
app.use('/api/ice', iceRouter);
app.use('/api/icebergs', icebergRouter);
app.use('/api/route', routingRouter);
app.use('/api/pipeline', pipelineRouter);
app.use('/api/weather', weatherRouter);
app.use('/api/sentinel1', sentinel1Router);
app.use('/api/collab', collabRouter); // SAR-RL 콜라보 (신규)
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

    // 서버 준비 후 헬스 체크 루프 시작
    setTimeout(scheduleHealthCheck, 30000);
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
  on: {
    proxyReq: (proxyReq, req) => {
      // express.json()이 body를 먼저 소비하므로 직접 재작성
      if (req.body && Object.keys(req.body).length > 0) {
        const bodyData = JSON.stringify(req.body);
        proxyReq.setHeader('Content-Type', 'application/json');
        proxyReq.setHeader('Content-Length', Buffer.byteLength(bodyData));
        proxyReq.write(bodyData);
      }
    },
    error: (_err, _req, res) => {
      res.status(503).json({
        error: 'RL 서버에 연결할 수 없습니다.',
        fallback: true,
        detail: rlProcess ? 'RL 서버 시작 중...' : 'RL 서버가 비활성화되어 있습니다.',
      });
    },
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
  on: {
    proxyReq: (proxyReq, req) => {
      if (req.body && Object.keys(req.body).length > 0) {
        const bodyData = JSON.stringify(req.body);
        proxyReq.setHeader('Content-Type', 'application/json');
        proxyReq.setHeader('Content-Length', Buffer.byteLength(bodyData));
        proxyReq.write(bodyData);
      }
    },
    error: (_err, _req, res) => {
      res.status(503).json({
        error: 'Report 서버에 연결할 수 없습니다.',
        fallback: true,
        detail: reportProcess ? 'Report 서버 시작 중...' : 'Report 서버가 비활성화되어 있습니다.',
      });
    },
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
  on: {
    proxyReq: (proxyReq, req) => {
      if (req.body && Object.keys(req.body).length > 0) {
        const bodyData = JSON.stringify(req.body);
        proxyReq.setHeader('Content-Type', 'application/json');
        proxyReq.setHeader('Content-Length', Buffer.byteLength(bodyData));
        proxyReq.write(bodyData);
      }
    },
    error: (_err, _req, res) => {
      res.status(503).json({
        error: 'ML 연료 예측 서버에 연결할 수 없습니다.',
        fallback: true,
        detail: mlProcess ? 'ML 서버 시작 중...' : 'ML 서버가 비활성화되어 있습니다.',
      });
    },
  },
}));

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
  });
}

// ── Weather pipeline scheduler ───────────────────────────────────
const WEATHER_SCRIPT_PATH = path.join(
  __dirname, '..', 'pipeline', 'fetchers', 'weather_fetcher.py'
);

function runWeatherPipeline() {
  console.log('[Scheduler] Running weather pipeline (Open-Meteo, all routes)...');
  const { cmd, args } = uvCommand([WEATHER_SCRIPT_PATH]);
  execFile(cmd, args, { env: uvEnv(), timeout: 180000 }, (err, stdout, stderr) => {
    if (err) console.error('[Scheduler] Weather pipeline error:', err.message);
    if (stdout) console.log('[Weather]', stdout.trim().slice(-500));
    if (stderr) console.error('[Weather] stderr:', stderr.trim().slice(-200));
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

  // CPU 사용량 (wmic)
  exec('wmic cpu get loadpercentage /value', (err, stdout) => {
    const cpuMatch = stdout && stdout.match(/LoadPercentage=(\d+)/);
    const cpu = cpuMatch ? cpuMatch[1] + '%' : 'N/A';

    // GPU 사용량 (nvidia-smi)
    exec('nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits', (gpuErr, gpuStdout) => {
      const gpu = (!gpuErr && gpuStdout.trim()) ? gpuStdout.trim() + '%' : 'N/A';
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

app.listen(PORT, () => {
  console.log(`[Server] Arctic Digital Twin API running on http://localhost:${PORT}`);
  console.log(`[Scheduler] Sentinel-1: 01:00 UTC | Ice: 02:00 UTC | SAR: 03:00 UTC | Berg: 04:00 UTC | Weather: every 6h`);
  // RL 파이프라인 자동 기동
  startRLServer();
  // Report 서비스 자동 기동
  startReportServer();
  // ML 연료 예측 서비스 자동 기동
  startMLServer();
});

// 프로세스 종료 시 모든 서버 정리
function cleanupProcesses() {
  rlServer.kill();
  reportServer.kill();
  mlServer.kill();
}
process.on('exit', cleanupProcesses);
process.on('SIGINT', () => { cleanupProcesses(); process.exit(); });
process.on('SIGTERM', () => { cleanupProcesses(); process.exit(); });
