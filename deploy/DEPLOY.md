# 🚀 Backend 배포 가이드 — AWS EC2 (단일 컨테이너)

이 백엔드는 **Node 게이트웨이(8000) 하나가 진입점**이고, 부팅 시 Python AI 서버
3종(RL 8001 / Report 8002 / Fuel 8003)을 자식 프로세스로 자동 기동한다.
따라서 **컨테이너 한 개**만 상시 띄우면 된다 (스케줄러 fetcher 포함 상시 구동 전제).

---

## 1. EC2 인스턴스 준비

| 항목 | 권장값 | 이유 |
|------|--------|------|
| 인스턴스 타입 | **`t3.medium` (2vCPU / 4GB)** | 추론 전용(학습 안 함) 데모 기준. 베이스라인 2~3GB + swap 6GB 로 보강. 여유 원하면 `t3.large` 8GB |
| 리전 | **us-east-1** (버지니아 북부) | 최저가 |
| OS | Ubuntu 22.04 / 24.04 LTS | setup 스크립트 기준 |
| 디스크 | gp3 **25GB** | 이미지 ~6GB + 모델 591MB + 캐시 |
| swap | **6GB** (setup 스크립트가 자동 생성) | 4GB RAM 부족분 + 부팅 동시 로드/fetcher 스파이크 흡수 → OOM 방지 |
| 보안그룹 인바운드 | `22`(SSH, 내 IP), `8000`(임시) 또는 `80/443`(권장) | 아래 2가지 노출 방식 참고 |

> **추론 전용 전제**: 학습(ML/RL)은 **로컬에서만** 돌리고 결과 모델 파일만 서버로 푸시한다.
> 서버는 `/api/*/train` 미사용 — **학습된 모델을 로드해 추론만** 하므로 4GB + swap 6GB 로 데모 가능하다.
> 부팅 시 AI 서버 3종의 동시 모델 로드 피크는 **순차 기동(stagger)** 으로 분산했다
> (`src/index.js`, 기본 25s 간격 → 약 50s 에 전부 기동; `AI_STARTUP_STAGGER_MS=0` 으로 동시 기동 복원).
> steady-state 베이스라인은 2~3GB 로 안정. 그래도 OOM-kill 이 보이거나 응답이 답답하면
> **t3.large(8GB)** 로 올리면 된다(인스턴스 타입만 변경, EBS·설정 그대로).

> 💸 **학생/가성비 팁**: 데모용이면 **안 쓸 때 인스턴스 stop** → EC2 요금 0,
> EBS만 과금(~$2/월). 필요할 때 start(1분). GitHub Student Pack 의 AWS 크레딧도 활용.

---

## 2. 부트스트랩

EC2 접속 후:

```bash
# 스크립트 방식 (Docker 설치 + 클론 + .env 템플릿까지)
curl -fsSL https://raw.githubusercontent.com/youmin0523/Arctic_Twin_Backend/main/deploy/ec2-setup.sh | bash

# .env 편집 (키 입력)
nano ~/Arctic_Twin_Backend/.env
#   ANTHROPIC_API_KEY=...        (What-If 분석)
#   COPERNICUS_MARINE_USER/_PASSWORD   (해양·기상 fetch)
#   CDSE_USER/CDSE_PASSWORD      (Sentinel-1 SAR)
#   DATABASE_URL=postgresql://...?sslmode=require   (Neon — 읽기 1차 소스)

# 빌드 + 기동
cd ~/Arctic_Twin_Backend
sudo docker compose up -d --build
```

> 키가 없는 기능은 해당 기능만 비활성/폴백되고 나머지는 정상 동작한다.
> `DATABASE_URL` 미설정 시 `backend/data/*.json` 파일 폴백으로 읽는다.

확인:
```bash
sudo docker compose ps
sudo docker compose logs -f backend          # AI 서버 3종 기동 로그
curl http://localhost:8000/api/health         # {"status":"ok",...}
curl http://localhost:8000/api/health/services # rl/report/ml 준비 상태
```

> AI 서버는 모델 로드에 1~3분 걸린다. `start_period` 동안 unhealthy 로 보이는 건 정상.

---

## 3. 외부 노출 방식 (둘 중 하나)

### (A) 빠른 테스트 — :8000 직접 노출
- 보안그룹에 `8000/tcp` 허용.
- Vercel rewrite 대상: `http://<EC2-퍼블릭-IP>:8000`
- 단점: 평문 HTTP, IP 고정 위해 **Elastic IP** 할당 권장.

### (B) 권장 — 도메인 + HTTPS (nginx 또는 Caddy)
1. Elastic IP 할당 → 도메인 A 레코드 연결 (예: `api.arctictwin.com`).
2. 보안그룹 `80`, `443` 허용 (8000 은 닫고 localhost 만).
3. compose 의 포트를 `"127.0.0.1:8000:8000"` 으로 좁힌 뒤 리버스 프록시:

```bash
# Caddy 예시 (자동 HTTPS) — /etc/caddy/Caddyfile
api.arctictwin.com {
    reverse_proxy 127.0.0.1:8000
}
```
- Vercel rewrite 대상: `https://api.arctictwin.com`

> 프론트(Vercel)는 브라우저→Vercel 구간이 HTTPS 이고 Vercel→백엔드는 서버사이드
> 프록시라 (A)의 평문 HTTP 도 mixed-content 없이 동작한다. 다만 운영은 (B) 권장.

---

## 4. 업데이트 / 운영

평상시 배포는 **GitHub Actions 가 자동**으로 한다(아래 6번). 수동 운영이 필요할 때:

```bash
cd ~/Arctic_Twin_Backend
git pull
sudo docker compose up -d --build     # 재빌드 후 무중단에 가깝게 교체
sudo docker compose logs -f backend
sudo docker compose down              # 정지
```

- 데이터/로그는 named volume(`backend-data`, `backend-logs`)에 유지된다.
- 모델은 git 에 포함(LFS 아님)되어 클론/풀로 함께 받아진다.
- 스케줄러(UTC): Sentinel-1 01시 / 해빙 02시 / SAR 03시 / berg 04시 / 기상 6시간마다.

---

## 5. 프론트엔드 연결

백엔드 주소가 정해지면 **frontend 레포의 `vercel.json`** 에서 호스트 토큰
`https://CHANGE-ME-AWS-BACKEND-HOST` 를 위 (A)/(B) 주소로 찾아바꾸기 하면 끝.
(프론트는 모든 호출을 상대경로로 하고 Vercel rewrite 가 이 백엔드로 넘긴다.)

---

## 6. CI/CD — 자동 배포 (GitHub Actions → EC2)

`backend/.github/workflows/deploy.yml` 가 **main 에 push 될 때마다** EC2 에 SSH 접속해
`git reset --hard origin/main` → `docker compose up -d --build` → 헬스 체크까지 수행한다.
(문서만(`*.md`) 바뀌면 건너뜀. Actions 탭에서 **수동 실행**(workflow_dispatch)도 가능.)

**최초 1회 준비:**

1. EC2 를 `deploy/ec2-setup.sh` 로 부트스트랩(Docker + clone + 빌드)해 둔다.
   이후부터는 Actions 가 `git reset --hard` + 재빌드만 한다.
2. backend 레포 **Settings → Secrets and variables → Actions** 에 등록:

| Secret | 필수 | 값 |
|---|---|---|
| `EC2_HOST` | ✅ | EC2 퍼블릭 IP 또는 도메인 (예: `13.51.x.x`, `api.arctictwin.com`) |
| `EC2_SSH_KEY` | ✅ | EC2 접속용 **개인키 전체** (`-----BEGIN ... END-----` 포함) |
| `EC2_USER` | — | 기본 `ubuntu` |
| `EC2_PORT` | — | 기본 `22` |
| `APP_DIR` | — | 기본 `~/Arctic_Twin_Backend` |

> 흐름: **로컬에서 학습 → 모델 커밋 → backend main push → Actions 가 EC2 갱신**.
> runtime 데이터(fetcher 결과)는 docker 볼륨에 분리돼 있어 `git reset --hard` 에 영향받지 않는다.
> 보안그룹에서 Actions 러너(외부 IP)가 SSH(22) 접속 가능해야 한다 — 22 를 막아뒀다면
> 배포용으로 임시 허용하거나 self-hosted 러너/Bastion 을 쓴다.
