#!/usr/bin/env bash
# ============================================================
# Arctic Digital Twin — EC2 Caddy 자동 HTTPS 설치 (멱등)
# ------------------------------------------------------------
# api.arctictwin.com 의 TLS 를 Caddy(Let's Encrypt)로 자동 종단하고
# 평문 백엔드(127.0.0.1:8000)로 리버스 프록시한다.
#
# 실행(기존 인스턴스에 1회):
#     cd ~/Arctic_Twin_Backend && sudo bash deploy/setup-caddy.sh
#
# 사전 준비 (스크립트 실행 전에 끝나 있어야 함):
#   1) DNS:  api.arctictwin.com  A 레코드 → EC2 Elastic IP
#            (Caddy 가 도메인 소유 검증을 위해 80 포트로 ACME 챌린지를 받음)
#   2) 보안그룹 인바운드: 80/tcp, 443/tcp 허용
#   3) 백엔드가 127.0.0.1:8000 에서 응답 중 (docker compose up -d)
#
# 변수(필요 시 override):  DOMAIN, BACKEND
#   sudo DOMAIN=api.example.com BACKEND=127.0.0.1:8000 bash deploy/setup-caddy.sh
# ============================================================
set -euo pipefail

DOMAIN="${DOMAIN:-api.arctictwin.com}"
BACKEND="${BACKEND:-127.0.0.1:8000}"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ root 권한 필요:  sudo bash deploy/setup-caddy.sh"
  exit 1
fi

echo "==> [1/5] Caddy 설치 (공식 apt 저장소, 멱등)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y
  apt-get install -y caddy
else
  echo "    caddy 이미 설치됨 — 건너뜀 ($(caddy version | head -1))"
fi

echo "==> [2/5] Caddyfile 배치 (도메인=${DOMAIN}, 백엔드=${BACKEND})"
mkdir -p /var/log/caddy
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CADDYFILE="${SCRIPT_DIR}/Caddyfile"
if [ -f "$SRC_CADDYFILE" ] && [ "$DOMAIN" = "api.arctictwin.com" ] && [ "$BACKEND" = "127.0.0.1:8000" ]; then
  # 기본 도메인/백엔드면 레포의 Caddyfile 을 그대로 사용 (버전관리됨)
  install -m 0644 "$SRC_CADDYFILE" /etc/caddy/Caddyfile
else
  # override 된 경우 동적으로 생성
  cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
	reverse_proxy ${BACKEND} {
		transport http {
			read_timeout 180s
			write_timeout 180s
		}
	}
	request_body {
		max_size 8MB
	}
	log {
		output file /var/log/caddy/${DOMAIN}.log
		format console
	}
}
EOF
fi

echo "==> [3/5] Caddyfile 문법 검증"
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

echo "==> [4/5] Caddy 기동/재적용 (인증서 자동 발급)"
systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "==> [5/5] 헬스 체크 (인증서 발급에 수십 초 걸릴 수 있음)"
echo "    로컬 백엔드:  $(curl -fsS -o /dev/null -w '%{http_code}' http://${BACKEND}/api/health 2>/dev/null || echo 'FAIL — 백엔드 먼저 기동하세요')"
echo ""
echo "✅ 완료. 1~2분 뒤 아래로 HTTPS 확인:"
echo "    curl -fsS https://${DOMAIN}/api/health"
echo "    sudo journalctl -u caddy -n 50 --no-pager   # 인증서 발급 로그"
echo ""
echo "⚠ 백엔드 8000 을 외부에 닫으려면 docker-compose.yml 포트를 '127.0.0.1:8000:8000' 로,"
echo "  보안그룹에서 8000/tcp 인바운드를 제거하세요 (Caddy 가 443 으로 대신 노출)."
