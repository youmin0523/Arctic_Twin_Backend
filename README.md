# 🧊 Arctic Digital Twin

> 북극항로 통항을 위한 AI 기반 디지털 트윈 플랫폼
> 강화학습 빙산 회피 + 실시간 해빙 모니터링 + 항로 최적화

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Vercel-000000?style=for-the-badge&logo=vercel)](https://digital-twin-omega-umber.vercel.app)
[![Backend API](https://img.shields.io/badge/Backend%20API-HuggingFace-FFD21E?style=for-the-badge&logo=huggingface)](https://heejin-oh-arctic-digital-twin-backend.hf.space)
[![Domain](https://img.shields.io/badge/Domain-arctic--twin.xyz-blue?style=for-the-badge)](https://arctic-twin.xyz)

---

## 🌐 Live Demo

| 서비스 | URL |
|--------|-----|
| **Frontend (Vercel)** | https://digital-twin-omega-umber.vercel.app |
| **Custom Domain** | https://arctic-twin.xyz |
| **Backend API (HF Spaces)** | https://heejin-oh-arctic-digital-twin-backend.hf.space |
| **Health Check** | [/health](https://heejin-oh-arctic-digital-twin-backend.hf.space/health) |

---

## ✨ 핵심 기능

### 🤖 AI 모델 4종 통합

| 기능 | 모델 | 역할 |
|------|------|------|
| **빙산 회피** | Stable Baselines3 (SAC) | 강화학습 기반 실시간 충돌 회피 경로 생성 |
| **출항 스케줄링** | Stable Baselines3 (SAC) | RL 기반 최적 출항 시점 결정 |
| **빙산 탐지** | YOLOv8 | SAR 위성 영상에서 빙산 자동 탐지 |
| **연료 예측** | XGBoost | 빙해 저항 기반 연료 소비량 회귀 |
| **What-If 분석** | Claude Agent SDK + Pool | LLM 기반 시나리오 자동 생성 |

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
                  │  arctic-twin.xyz                  │
                  └────────────┬─────────────────────┘
                               │ /api/* rewrites
                               ▼
              ┌──────────────────────────────────────────┐
              │  Hugging Face Spaces (Backend Container)  │
              │  ┌──────────────────────────────────────┐ │
              │  │  FastAPI Proxy (port 7860)            │ │
              │  └──────┬─────────┬─────────┬──────────┘ │
              │         │         │         │             │
              │  ┌──────▼──┐ ┌────▼────┐ ┌─▼──────┐ ┌──▼─┐│
              │  │ rl-pipe │ │ report  │ │ ml-pipe│ │ sar││
              │  │ (8001)  │ │ (8002)  │ │ (8003) │ │8005││
              │  └─────────┘ └─────────┘ └────────┘ └────┘│
              │       SAC      Claude SDK   XGBoost  YOLOv8│
              └──────────────────────────────────────────┘
                               │
                               ▼
              ┌────────────────────────────────────────┐
              │  External APIs                         │
              │  - Copernicus Marine (해양 데이터)      │
              │  - CDSE (Sentinel-1 SAR)               │
              │  - NSIDC (해빙 농도)                    │
              │  - Anthropic Claude (시나리오 생성)     │
              └────────────────────────────────────────┘
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
- **Container**: Docker (multi-process)
- **Hosting**: Hugging Face Spaces (CPU 16GB Free)

### Data
- **위성 데이터**: Sentinel-1 SAR (Copernicus CDSE)
- **해빙 농도**: NSIDC (NASA)
- **기상 데이터**: Copernicus Marine Service
- **모델 저장**: Git LFS (~200MB)

### DevOps
- **CI/CD**: GitHub → Vercel/HF Spaces 자동 배포
- **Secrets**: HF Secrets (API keys)
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

| 키 | 용도 | 없을 때 영향 |
|---|---|---|
| `ANTHROPIC_API_KEY` | What-If Claude 분석 | What-If 탭 비활성 |
| `COPERNICUS_MARINE_USER` / `_PASSWORD` | 실시간 해양 데이터 | 캐시된 데이터 사용 |
| `CDSE_USER` / `CDSE_PASSWORD` | Sentinel-1 SAR | 샘플 이미지 사용 |

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
- http://127.0.0.1:8002/api/report/health  → `rl_model_loaded: true` 떠야 정상
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

| 증상 | 원인 / 해결 |
|------|------------|
| `ECONNREFUSED` 가 vite proxy 에서 뜸 | 해당 백엔드가 안 떠있음. Node(step 4)·AI 서버(step 5) 확인. |
| 8002 가 `rl_model_loaded: false` | `backend/model/report-service/*.onnx` 가 안 받아진 거 — `git lfs pull` 또는 git pull 재시도 |
| `ModuleNotFoundError: gymnasium` 등 | step 3 의존성 설치 누락. `uv pip install --python backend/.venv -r backend/requirements.txt` 재실행. |
| Python 3.14 에서 `NoEventLoopError` | 이미 패치됨. `anyio.to_thread` / `BackgroundTask` 가 server.py 상단에서 monkey-patch. |
| What-If 가 0% 멈춤 | `ANTHROPIC_API_KEY` 가 backend/.env 에 있는지 확인. |

---

## 📦 배포

### Hugging Face Spaces (백엔드 + AI)
[`hf-space/DEPLOY.md`](hf-space/DEPLOY.md) 참고. Docker 기반 자동 빌드.

### Vercel (프론트엔드)
1. GitHub 저장소 import
2. Root Directory: `frontend`
3. Framework: Vite
4. 자동 배포

---

## 🎯 프로젝트 하이라이트

### 1. 듀얼 클라우드 아키텍처
- **프론트엔드**: Vercel (Edge Network)
- **백엔드 + AI**: Hugging Face Spaces (16GB RAM)
- **장점**: 각 플랫폼의 강점 활용, 영구 무료 운영

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

- GitHub: [@Hijin554](https://github.com/Hijin554)
- Live Demo: https://arctic-twin.xyz
