# 🧊 Arctic Digital Twin

> 북극항로 통항을 위한 AI 기반 디지털 트윈 플랫폼
> 강화학습 빙산 회피 + 실시간 해빙 모니터링 + 항로 최적화

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Vercel-000000?style=for-the-badge&logo=vercel)](https://digital-twin-omega-umber.vercel.app)
[![Backend API](https://img.shields.io/badge/Backend%20API-AWS%20EC2-FF9900?style=for-the-badge&logo=amazonaws)](https://arctictwin.com)
[![Domain](https://img.shields.io/badge/Domain-arctictwin.com-blue?style=for-the-badge)](https://arctictwin.com)

---

## 🌐 Live Demo

| 서비스                      | URL                                                                      |
| --------------------------- | ------------------------------------------------------------------------ |
| **Frontend (Vercel)**       | https://digital-twin-omega-umber.vercel.app                              |
| **Custom Domain**           | https://arctictwin.com                                                   |
| **Backend API (AWS EC2)**   | 배포 도메인 설정 후 업데이트 (Vercel `/api/*` rewrite 경유)              |
| **Health Check**            | `<백엔드 도메인>/health`                                                  |

---

## ✨ 핵심 기능

### 🤖 AI 모델 4종 통합

| 기능              | 모델                    | 역할                                     |
| ----------------- | ----------------------- | ---------------------------------------- |
| **빙산 회피**     | Stable Baselines3 (SAC) | 강화학습 기반 실시간 충돌 회피 경로 생성 |
| **출항 스케줄링** | Stable Baselines3 (SAC) | RL 기반 최적 출항 시점 결정              |
| **빙산 탐지**     | YOLOv8                  | SAR 위성 영상에서 빙산 자동 탐지         |
| **연료 예측**     | XGBoost                 | 빙해 저항 기반 연료 소비량 회귀          |
| **What-If 분석**  | Claude Agent SDK + Pool | LLM 기반 시나리오 자동 생성              |

### 🎨 인터랙티브 프론트엔드

- **Cesium.js 3D 지도** - 북극 지역 위성 영상 + 실시간 해빙 농도
- **Deck.gl 시각화** - 빙산 위치 + 항로 오버레이
- **Three.js** - 선박 3D 모델
- **React + Vite** - 빠른 개발 환경

---

## 🏗️ 아키텍처

```
                            ┌─────────────────┐
                            │     User        │
                            └────────┬────────┘
                                     │ HTTPS
                                     ▼
                  ┌──────────────────────────────────┐
                  │  Vercel (Frontend - React+Cesium) │
                  │  arctictwin.com                   │
                  └────────────┬─────────────────────┘
                               │ /api/* rewrites
                               ▼
              ┌──────────────────────────────────────────┐
              │  AWS EC2 (Ubuntu · PM2)                    │
              │  Nginx :443 → Node API Gateway :8000       │
              │  └──────┬─────────┬─────────┬──────────┐   │
              │         │         │         │          │   │
              │  ┌──────▼──┐ ┌────▼────┐ ┌─▼──────┐ ┌──▼─┐ │
              │  │ rl-pipe │ │ report  │ │ ml-pipe│ │ sar│ │
              │  │ (8001)  │ │ (8002)  │ │ (8003) │ │8005│ │
              │  └─────────┘ └─────────┘ └────────┘ └────┘ │
              │       SAC      Claude SDK   XGBoost  YOLOv8 │
              └──────────────────┬───────────────────────┘
                       │ DATABASE_URL │
                       ▼              ▼
        ┌────────────────────┐  ┌────────────────────────────┐
        │ Neon PostgreSQL    │  │  External APIs              │
        │ (빙산·SAR·sentinel │  │  - Copernicus Marine        │
        │  ·시뮬레이션 DB)   │  │  - CDSE (Sentinel-1 SAR)    │
        │ + 파일 폴백         │  │  - NSIDC (해빙 농도)        │
        └────────────────────┘  │  - Anthropic Claude         │
                                 └────────────────────────────┘
```

---

## 🛠️ 기술 스택

### Frontend

- **Framework**: React 18 + Vite
- **3D**: Cesium.js, Three.js, Deck.gl
- **Charts**: Recharts
- **Hosting**: Vercel

### Backend

- **API Framework**: FastAPI (Python 3.11)
- **ML/AI**:
  - PyTorch 2.x + Stable Baselines3 (강화학습)
  - Ultralytics YOLOv8 (Computer Vision)
  - XGBoost (회귀)
  - Claude Agent SDK + MCP (LLM 도구)
- **Process 관리**: PM2 / systemd (Node 진입점이 Python 서비스 자식 프로세스 기동)
- **Hosting**: AWS EC2 (Ubuntu, t2.medium 4GB + swap 6GB / 추론 데모) + Nginx 리버스 프록시

### Data

- **위성 데이터**: Sentinel-1 SAR (Copernicus CDSE)
- **해빙 농도**: NSIDC (NASA)
- **기상 데이터**: Copernicus Marine Service
- **정형 데이터 DB**: Neon PostgreSQL (빙산·SAR·sentinel1·시뮬레이션) — 파일 폴백 지원
- **모델 저장**: Git LFS (~200MB)

### DevOps

- **CI/CD**: GitHub → Vercel(프론트) 자동 배포 / EC2(백엔드) git pull + PM2 reload
- **Secrets**: EC2 `backend/.env` (API keys, DATABASE_URL)
- **DNS**: Gabia + Vercel
- **버전 관리**: Git LFS

---

## 🚀 로컬 실행 (팀원용 셋업 가이드)

### 0. 사전 준비

- **Python 3.11 이상** (3.14도 OK — async 패치 적용됨)
- **[uv](https://docs.astral.sh/uv/)** — 모든 백엔드 Python 실행/환경을 uv 로 통일 (`pip install uv` 또는 `winget install astral-sh.uv`)
- **Node.js 20 이상**
- **Git** (모델 파일이 ~600MB 라 pull 시간 좀 걸림)

### 1. 저장소 클론

```bash
git clone https://github.com/Hijin554/digital-twin.git
cd digital-twin
git checkout Hijin   # 작업 브랜치
```

클론 직후 다음이 있어야 합니다:

- `backend/` — Node.js(Express) API 게이트웨이 (포트 8000) — 해빙·빙산·항로·기상 API + 정적 데이터
- `backend/model/` — 학습된 모델 47개 (RL 회피 9 + 출항 ONNX 29 + 항법 6 + fuel 1 + yolo 1, 총 ~591MB)
- `backend/data/` — 해빙·기상 데이터 일부 (없는 데이터는 외부 API에서 자동 fetch)
- `backend/services/` — 3개 AI 서비스 소스 (rl-pipeline / report-service / ml-pipeline)
- `sar_server.py` — SAR 빙산 탐지 서버 (포트 8005)
- `services-launcher/` — AI 서버 launcher(.ps1) + 모델 테스트 스크립트
- `tools/` — 개발·학습·모니터링 유틸 스크립트 (모니터·워치독·트리거 등)
- `frontend/` — React + Vite (포트 5173)

### 2. 환경변수 (선택)

```bash
cp backend/.env.example backend/.env
# .env 편집 — 키가 없으면 해당 기능만 비활성화, 다른 건 정상 동작
```

| 키                                     | 용도                                                        | 없을 때 영향                                        |
| -------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------- |
| `ANTHROPIC_API_KEY`                    | What-If Claude 분석                                         | What-If 탭 비활성                                   |
| `COPERNICUS_MARINE_USER` / `_PASSWORD` | 실시간 해양 데이터                                          | 캐시된 데이터 사용                                  |
| `CDSE_USER` / `CDSE_PASSWORD`          | Sentinel-1 SAR                                              | 샘플 이미지 사용                                    |
| `DATABASE_URL`                         | Neon PostgreSQL (빙산·SAR·sentinel1·시뮬레이션 정형 데이터) | **`backend/data/*.json` 파일로 자동 폴백** (무중단) |

### 3. Python 의존성 설치 (uv 단일 환경)

모든 백엔드 Python(rl/report/ml/sar 서비스 + 데이터 파이프라인)은 **`backend/.venv` 하나**를 공유하고, 의존성은 **`backend/requirements.txt` 한 파일**로 통합되어 있습니다. uv 로 한 번만 설치하면 됩니다:

```bash
# 저장소 루트에서
uv venv backend/.venv
uv pip install --python backend/.venv -r backend/requirements.txt
```

이후 서비스 실행은 전부 uv 가 이 환경을 사용합니다 (services-launcher, Node 자동 기동, .bat 모두 동일). 서비스별 개별 `venv/` 는 더 이상 만들지 않습니다.

> 💡 의존성을 추가할 땐 `backend/requirements.txt` 에 적고 위 `uv pip install` 을 다시 실행하세요.

### 4. Node API 백엔드 (포트 8000)

프론트엔드의 해빙·빙산·항로·기상 API와 정적 데이터(`/data`)는 이 서버가 담당합니다. **AI를 배포본(HF Space)으로 쓸 거면 이 단계 + 프론트엔드(step 7)만으로 화면이 동작합니다.**

```bash
cd backend
npm install
npm run dev        # → http://localhost:8000  (정지: Ctrl+C)
```

> ⚠️ **로컬 AI 구동 방법은 둘 중 하나만 고르세요.** Node 백엔드는 공용 환경 `backend/.venv` 가 **있으면** RL/Report/Fuel(8001~8003)을 `uv run` 으로 자동 기동합니다.
>
> - **(A) Node 자동 기동**: step 3 으로 `backend/.venv` 만들어두고 위 `npm run dev` → 8001~8003 자동 실행.
> - **(B) 수동 기동**: 아래 step 5 의 `services-launcher` 로 직접 실행. (launcher 도 `backend/.venv` 없으면 자동 생성)
>
> (A)·(B)를 동시에 켜면 같은 포트를 두 번 잡아 충돌합니다. AI 없이 배포본만 쓸 거면 `backend/.venv` 를 안 만들면 됩니다.

### 5. AI 서버 직접 띄우기 (로컬 AI — 선택, 방법 B)

각각 별도 PowerShell/터미널 창에서:

```powershell
# 창 1 — RL 회피 (8001)
.\services-launcher\start-rl-pipeline.ps1

# 창 2 — Report (8002)
.\services-launcher\start-report-service.ps1

# 창 3 — Fuel (8003)
.\services-launcher\start-ml-pipeline.ps1

# 창 4 — SAR (8005)
.\services-launcher\start-sar-server.ps1
```

> macOS/Linux (PowerShell 없이 직접 실행): `VIRTUAL_ENV=backend/.venv uv run --no-project --active uvicorn server:app --host 127.0.0.1 --port <port>` (해당 서비스 폴더에서). SAR 는 루트에서 `... uv run --no-project --active python sar_server.py`.

각 서버 health check:

- http://127.0.0.1:8001/api/rl/health
- http://127.0.0.1:8002/api/report/health → `rl_model_loaded: true` 떠야 정상
- http://127.0.0.1:8003/api/fuel/health
- http://127.0.0.1:8005/api/sar/status

### 6. AI 모델 검증 (서버 없이도 가능)

`services-launcher/` 의 테스트 스크립트로 모든 모델 로드·추론 한 번에 확인 (공용 환경 사용):

```powershell
$env:VIRTUAL_ENV = "backend/.venv"   # bash: export VIRTUAL_ENV=backend/.venv

# 9개 회피 모델 (NSR/NWP/TSR × easy/normal/hard)
uv run --no-project --active python services-launcher/test_avoidance_models.py

# 29개 출항 ONNX 모델 (IA/PC3~7 × bulk/container/lng/tanker)
uv run --no-project --active python services-launcher/test_departure_models.py
```

✅ 정상이면 `OK 9/9`, `OK 29/29` 로 끝남.

### 7. 프론트엔드 실행

```bash
cd frontend
npm install
npm run dev
```

브라우저에서 http://localhost:5173 열기.

**프록시 설정** (`frontend/vite.config.js` 상단):

- 기본값: `/api/rl`, `/api/report`, `/api/fuel` → HF Space (배포본)
- `/api/sar` → localhost:8005 (로컬 서버)
- `/api`(해빙·항로 등 일반)·`/data`·`/proxy` → localhost:8000 (Node 백엔드, step 4)
- 로컬 백엔드로 테스트하려면 해당 상수를 `'http://localhost:<port>'` 로 바꾸기

### 8. 문제 해결

| 증상                                 | 원인 / 해결                                                                                          |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `ECONNREFUSED` 가 vite proxy 에서 뜸 | 해당 백엔드가 안 떠있음. Node(step 4)·AI 서버(step 5) 확인.                                          |
| 8002 가 `rl_model_loaded: false`     | `backend/model/report-service/*.onnx` 가 안 받아진 거 — `git lfs pull` 또는 git pull 재시도          |
| `ModuleNotFoundError: gymnasium` 등  | step 3 의존성 설치 누락. `uv pip install --python backend/.venv -r backend/requirements.txt` 재실행. |
| Python 3.14 에서 `NoEventLoopError`  | 이미 패치됨. `anyio.to_thread` / `BackgroundTask` 가 server.py 상단에서 monkey-patch.                |
| What-If 가 0% 멈춤                   | `ANTHROPIC_API_KEY` 가 backend/.env 에 있는지 확인.                                                  |

---

## 🗄️ 데이터 저장 — PostgreSQL + 파일 폴백

정형 데이터는 **Neon PostgreSQL** 을 우선 조회하고, DB 미설정·연결 실패 시 기존
`backend/data/*.json` 파일로 **자동 폴백**합니다(무중단). API 응답 구조는 두 경로가 동일합니다.

### DB로 서빙하는 테이블

| 테이블               | 원본 파일                       | 읽기 경로                                              |
| -------------------- | ------------------------------- | ------------------------------------------------------ |
| `icebergs`           | `copernicus_icebergs.json`      | `GET /api/icebergs/latest`, `/api/collab/all-icebergs` |
| `bergs`              | `realBergData_latest.json`      | `GET /api/icebergs/latest`                             |
| `sentinel1_products` | `sentinel1_catalog_latest.json` | `GET /api/sentinel1/catalog`, `/products`              |
| `sar_detections`     | `sar_detections_latest.json`    | `GET /api/collab/sar-icebergs`, `/sar-metadata`        |
| `simulation_results` | `data/simulations/*.json`       | `GET /api/simulations`, `/api/simulations/:scenario`   |
| `edited_routes`      | `edited_routes.json`            | `GET/POST /api/routes/edited` (DB 우선·파일 폴백·미러)  |

> 대용량 해빙 그리드(`realIceData_*`)와 기상(`weather_latest.json`)은 의도적으로 **파일 유지**(DB 미이관).

### 동작 방식

- **읽기**: Node(`src/services/db.js`, `dataStore.js`, `sarDetectionStore.js`)와 Python report-service(`modules/db.py`, `data_loader.py`, `whatif_tools.py`)가 DB 우선 + 파일 폴백.
- **쓰기/동기화**: 데이터 수집 fetcher 가 JSON 을 갱신하면, `src/index.js` 가 **fetcher 완료 후 + 서버 시작 시** `scripts/sync_db.js` 를 자동 실행해 DB 를 최신화(멱등 upsert). SAR 온디맨드 탐지 후에도 자동 동기화.
- **스키마/마이그레이션**: `scripts/schema.sql` + `scripts/sync_db.js` (backend 단독 동작 — `backend/node_modules` 의 `pg`·`dotenv` 사용, 루트 `database/` 폴더 의존 없음).

### 설정 & 수동 동기화

```bash
# backend/.env 에 DATABASE_URL 설정 (Neon 연결 문자열, sslmode=require 포함)
#   미설정 시 자동으로 JSON 파일 폴백 모드로 동작

cd backend
node scripts/sync_db.js        # JSON → DB 수동 동기화 (스키마 적용 + upsert, 재실행 안전)
```

> Node 는 `pg`, Python report-service 는 `psycopg2-binary` 를 사용하며 둘 다 통합 의존성에 포함되어 있습니다.

---

## 🧠 AI 모델 관리 — MANIFEST + 무결성 검증

학습된 모델(YOLOv8 SAR 탐지기, SAC 회피/출항, XGBoost 연료 — 총 ~720MB)은
**`model/MANIFEST.json`** 을 단일 진실원본으로 버전·무결성 관리합니다. 각 모델의
경로·크기·**sha256**·역할(role)을 기록해 "어떤 모델이 포함돼야 하는가"를 명시하고,
클론·배포 시 누락(SAR 미포함 등)·손상을 자동으로 잡아냅니다.

```bash
cd backend
npm run models:verify     # 로컬 모델 ↔ MANIFEST 대조 (누락·손상 시 exit 1) — CI/배포 게이트
npm run models:generate   # 모델 추가/교체 후 MANIFEST 재생성 (커밋 대상)
npm run models:download    # MODEL_BASE_URL 에서 누락 모델 자동 다운로드
```

- **재현성**: `MANIFEST.json` 이 sha256 을 고정 → 팀원/서버가 동일 모델을 보장.
- **SAR 언블록**: YOLOv8 가중치가 누락되면 `verify` 가 즉시 식별, `download` 로 복구.
- **버전관리**: 모델 교체 시 `generate` → 해시 변경이 diff 로 드러나 추적 가능.
- **호스팅**: 대용량 바이너리는 git 대신 외부 스토리지(S3/HF 등)에 두고
  `MODEL_BASE_URL` 로 받는 것을 권장(`<base>/<manifest path>` 규칙). 무결성은 sha256 으로 검증.

> CI(`.github/workflows/ci.yml`)의 `node-tests` 잡이 MANIFEST 스키마·diff 로직을 테스트하고,
> 배포 전 `npm run models:verify` 로 모델 가용성을 게이트할 수 있습니다.

## 📦 배포

### AWS EC2 (백엔드 + AI)

백엔드는 **Node API 게이트웨이(8000)가 Python AI 서비스(8001~8005)를 자식 프로세스로 기동**하고
스케줄러·DB 동기화가 상시 동작하는 멀티프로세스 구조라, 장시간 실행되는 **단일 VM(EC2)** 에
배포하는 것이 적합합니다. (로컬 셋업과 동일한 흐름을 서버에서 재현)

**1) 인스턴스 준비**
- Ubuntu 22.04 LTS, 권장 `t2.medium`(2vCPU/4GB + swap 6GB — 추론 데모). 여유는 `t3.large` 8GB, 학습은 `t3.xlarge` 16GB. GPU는 선택.
- 스토리지 **40GB** (torch 이미지 압축해제 ~3GB + 모델 ~600MB + swap 6GB + 빌드 캐시; 25GB 는 빌드 실패).
- **보안 그룹**: 인바운드 80/443(웹)만 공개. 8000~8005 는 외부에 열지 말고 내부에서만 사용.

**2) 런타임 설치**
```bash
# Node.js 20, uv, git-lfs
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs git-lfs
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**3) 코드 + 의존성**
```bash
git clone <repo> && cd digital-twin && git lfs pull   # 모델 파일
uv venv backend/.venv
uv pip install --python backend/.venv -r backend/requirements.txt
cd backend && npm install
```

**4) 환경변수** — `backend/.env` 작성 (로컬과 동일 키 + 운영 DB)
```bash
DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require   # Neon PostgreSQL
ANTHROPIC_API_KEY=...
COPERNICUS_MARINE_USER=... / COPERNICUS_MARINE_PASSWORD=...
CDSE_USER=... / CDSE_PASSWORD=...
```

**5) 상시 구동 (PM2 또는 systemd)** — Node(8000)가 RL/Report/Fuel(8001~8003)을 자동 기동하므로
**진입점은 Node 하나만** 관리하면 됩니다. SAR(8005)만 별도 기동.
```bash
sudo npm i -g pm2
pm2 start backend/src/index.js --name arctic-api
pm2 start "uv run --no-project --active python sar_server.py" --name arctic-sar --cwd backend
pm2 save && pm2 startup     # 부팅 시 자동 시작
```

**6) 리버스 프록시 + HTTPS (Nginx + Certbot)** — `:80/:443` → `127.0.0.1:8000` 프록시.
`/api/rl`·`/api/report`·`/api/fuel` 등은 Node 가 내부에서 8001~8005 로 다시 프록시하므로
Nginx 는 8000 한 곳만 바라보면 됩니다.

> DB 는 관리형 **Neon PostgreSQL** 을 그대로 사용(별도 RDS 불필요). EC2 부팅·fetcher 실행 시
> `scripts/sync_db.js` 가 자동으로 JSON→DB 동기화합니다. `DATABASE_URL` 미설정 시 파일 폴백 동작.
>
> _(AWS EC2 단일 컨테이너 배포 상세는 [`deploy/DEPLOY.md`](deploy/DEPLOY.md) 참고)_

### Vercel (프론트엔드)

1. GitHub 저장소 import → Root Directory: `frontend`, Framework: Vite
2. `frontend/vercel.json` 의 `CHANGE-ME-AWS-BACKEND-HOST` 를 **EC2 도메인**으로 치환
   (`/api/*`·`/health` rewrite 대상). 상세는 `frontend/README.md` 참고.

---

## 🎯 프로젝트 하이라이트

### 1. 듀얼 클라우드 아키텍처

- **프론트엔드**: Vercel (Edge Network)
- **백엔드 + AI**: AWS EC2 (멀티프로세스 상시 구동) + Neon PostgreSQL (관리형 DB)
- **장점**: 장시간 실행 AI 서비스·스케줄러에 적합, 프론트는 글로벌 엣지 배포

### 2. 강화학습 모델 다수 학습

- 3 항로 × 7 빙해 등급 × 4 선박 종류 = **84개 빙산 회피 모델**
- 7 × 4 = **28개 출항 스케줄 모델**
- Stable Baselines3 SAC 기반 학습

### 3. Graceful Degradation 패턴

- 외부 LLM API (Claude) 실패 시 풀 시나리오로 자동 대체
- 운영 안정성 보장

### 4. Git LFS 관리

- 200MB+ AI 모델 파일 LFS 추적
- 코드 저장소 가벼움 유지

---

## 📊 프로젝트 통계

- **총 코드**: ~30,000 LOC
- **백엔드 서비스**: 5개 (Python FastAPI 4 + Node.js Express 1)
- **AI 모델**: 30+ (RL × 24, YOLOv8 × 1, XGBoost × 2)
- **외부 API 연동**: 4개 (Copernicus, CDSE, NSIDC, Anthropic)

---

## 🤝 팀

원본 저장소: [youmin0523/Digital_twin](https://github.com/youmin0523/Digital_twin)

---

## 📜 라이선스

MIT License

---

## 🙋 연락처

- GitHub: [@youmin0523](https://github.com/youmin0523)
- Live Demo: 추후 업데이트 예정
