#!/usr/bin/env bash
# ============================================================
# Arctic Digital Twin — AWS EC2 부트스트랩 (Ubuntu 22.04/24.04)
#   새 EC2 인스턴스에서 Docker + Compose 설치 → 레포 클론 → 빌드 → 기동.
#   사용:
#     curl -fsSL <this-file-raw-url> | bash
#   또는 인스턴스에 복사 후:
#     bash ec2-setup.sh
# ------------------------------------------------------------
#   권장 인스턴스: t3.medium (2vCPU / 4GB). 추론 전용(학습 안 함) 데모 기준.
#   PyTorch/YOLO/xgboost AI 서버 3종 + 모델 ~591MB 가 상주 (베이스라인 ~2~3GB).
#   4GB 는 빠듯하므로 swap 6G 로 부팅 동시 로드·fetcher 스파이크를 흡수한다.
#   (여유 원하면 t3.large 8GB, 학습까지 돌리면 t3.xlarge 16GB.)
#   디스크: gp3 25GB 이상 (이미지 ~6GB + 모델 + 데이터 캐시).
#   보안그룹: 8000/tcp 인바운드 허용 (또는 80/443 → nginx 프록시).
#   비용팁: 데모용이면 안 쓸 때 인스턴스 stop → EC2 요금 0 (EBS만 과금).
# ============================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/youmin0523/Arctic_Twin_Backend.git}"
APP_DIR="${APP_DIR:-$HOME/Arctic_Twin_Backend}"
SWAP_SIZE="${SWAP_SIZE:-6G}"   # t3.medium(4GB) 보강: RAM 부족분을 swap 으로 흡수

echo "==> [1/5] 시스템 패키지 + Docker 설치"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git
# Docker 공식 설치 스크립트
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true   # 로그아웃/재접속 후 sudo 없이 docker 사용

echo "==> [2/5] swap ${SWAP_SIZE} 설정 (t3.medium 4GB 보강 — 부팅 동시 로드/fetcher 스파이크 흡수)"
if ! sudo swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l "$SWAP_SIZE" /swapfile 2>/dev/null \
    || sudo dd if=/dev/zero of=/swapfile bs=1M count=6144
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
else
  echo "    swap 이미 존재 — 건너뜀"
fi

echo "==> [3/5] 레포 클론 (모델 포함 ~600MB, 시간 소요)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> [4/5] .env 준비"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    !! .env 생성됨 — 편집해서 키를 채우세요:  nano $APP_DIR/.env"
  echo "       (ANTHROPIC_API_KEY / COPERNICUS_* / CDSE_* / DATABASE_URL)"
  echo "    채운 뒤 다시 실행:  cd $APP_DIR && sudo docker compose up -d --build"
  exit 0
fi

echo "==> [5/5] 빌드 + 기동"
sudo docker compose up -d --build

echo ""
echo "==> 완료. 상태 확인:"
echo "    sudo docker compose ps"
echo "    sudo docker compose logs -f backend"
echo "    curl http://localhost:8000/api/health"
