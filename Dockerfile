# ============================================================
# Arctic Digital Twin — Backend (단일 컨테이너)
#   Node.js 게이트웨이(8000) + Python AI 서비스(RL 8001 / Report 8002 / Fuel 8003)
#   Node 가 부팅 시 Python 서버들을 자식 프로세스로 자동 기동한다 (src/index.js).
#   AWS EC2 단일 인스턴스에서 docker / docker compose 로 상시 구동하는 것을 전제로 한다.
# ============================================================
FROM node:20-bookworm-slim

# --- 런타임 시스템 라이브러리 ---
#   libgomp1        : torch / xgboost / scikit-learn 의 OpenMP 런타임
#   libglib2.0-0    : opencv(headless) / ultralytics 의존
#   ca-certificates : 외부 API(Copernicus/CDSE/Anthropic) HTTPS
#   curl            : uv 설치 + HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# --- uv 설치 (astral 공식 이미지에서 정적 바이너리 복사) ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV NODE_ENV=production \
    PORT=8000 \
    # uv 가 backend/.venv 를 공용 환경으로 쓰도록 (uvPython.js 와 동일 규약)
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- 1) Node 의존성 (레이어 캐시: package*.json 만 먼저) ---
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# --- 2) Python 단일 환경(.venv) + 의존성 ---
#   uv 가 관리하는 CPython 3.11 을 받아 backend/.venv 생성.
#   torch/torchvision 은 CPU 휠로 받아 이미지 크기를 줄인다
#   (==2.11.0 핀은 PEP440 상 2.11.0+cpu 로컬버전과 매칭됨).
COPY requirements.txt ./
#   --index-strategy unsafe-best-match: torch CPU 휠은 pytorch 인덱스, 그 외(idna 등)는
#   PyPI 에서 핀 버전을 찾도록 모든 인덱스에서 최적 버전을 선택 (단일 인덱스 정책 우회).
RUN uv venv .venv --python 3.11 \
    && uv pip install --python .venv \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        -r requirements.txt

# --- 3) 애플리케이션 소스 + 학습 모델(~591MB) ---
COPY . .

EXPOSE 8000

# Node 게이트웨이 헬스체크 (Python 서버 기동 여유 위해 start-period 넉넉히)
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["node", "src/index.js"]
