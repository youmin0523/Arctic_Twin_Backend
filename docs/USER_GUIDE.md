# Arctic Digital Twin — 사용 설명서

북극항로(NSR) 디지털 트윈의 **Live Simulation** · **Voyage Playback** 두 모드 사용법.

---

## 목차

1. [한 줄 요약](#한-줄-요약)
2. [모드 비교 — Live vs Voyage](#모드-비교--live-vs-voyage)
3. [Live Simulation 모드](#live-simulation-모드)
4. [Voyage Playback 모드](#voyage-playback-모드)
5. [카메라 뷰 (공통)](#카메라-뷰-공통)
6. [UI 패널 설명](#ui-패널-설명)
7. [선박 거동 (흔들림) 시스템](#선박-거동-흔들림-시스템)
8. [이벤트 & 자동 전환](#이벤트--자동-전환)
9. [빠른 워크플로우 예시](#빠른-워크플로우-예시)
10. [자주 묻는 질문 (FAQ)](#자주-묻는-질문-faq)
11. [한계와 주의사항](#한계와-주의사항)
12. [개발자 참조](#개발자-참조)

---

## 한 줄 요약

- **Live** = 실시간 위성 데이터 + 자유 조작 → **"지금 어떻게 할까?" 의사결정 도구**
- **Voyage** = 백엔드가 미리 계산한 항해 시뮬 재생 → **"이 항해 전체가 어떻게 펼쳐지나?" 분석·검증·시연 도구**

둘 다 있어야 디지털 트윈의 진짜 가치가 나와. 한쪽만 쓰는 건 반쪽만 쓰는 것.

---

## 모드 비교 — Live vs Voyage

| 항목 | 🔴 Live Simulation | 🟢 Voyage Playback |
|---|---|---|
| **소스 데이터** | Copernicus/NSIDC 실시간 위성 해빙 + Open-Meteo 실시간 기상 | 백엔드가 미리 계산한 trace JSON (`simulate_voyage` 출력) |
| **선박 위치** | `state.shipState` — 자유 제어 | trace 파일에 고정 (시간 따라 재생) |
| **선박 조작** | 수동 조종(WASD) / 텔레포트 / 자동 항해 | ❌ 불가 (재생 전용) |
| **시간** | 실시간 흐름 | tHours 축 (seek로 자유 이동) |
| **얼음 데이터** | 라이브 해빙 격자 (농도 %) | trace 의 `thickness_m`·`effective_thickness_m`·`rio` |
| **파고·파향** | weather_latest.json 최근접 waypoint | weather_latest.json 최근접 waypoint (동일) |
| **아라온** | Wrangel Island 정박 마커만 (정적) | 시뮬 로직 따라 동적 (출동·호위·복귀) |
| **이벤트** | 없음 | `call`·`rendezvous`·`start_escort`·`release`·`return`·`intercept_failed`·`arrive` |
| **What-if 비교** | ❌ (배 1척만 가능) | ✅ Arc4/Arc7/Arc9 trace 번갈아 비교 |
| **시간 압축** | ❌ (실시간만) | ✅ 1x / 10x / 60x / 300x |
| **시간 되감기** | ❌ | ✅ seek bar |
| **쇄빙 저항/연비 차트** | 스냅샷만 (현재 값) | 과거 12h 라인차트 |
| **이벤트 자동 전환** | ❌ | ✅ VoyageAutoCam |
| **사고 재생·사후 분석** | ❌ | ✅ (실제 로그를 trace 포맷으로 변환 후 재생) |
| **주 용도** | 실시간 의사결정, 수동 운항 훈련 | 모델 검증, 선급 비교, 시연, 교육, 사후 분석 |

### 언제 어느 걸 써?

| 상황 | 권장 모드 |
|---|---|
| "지금 북극 항해 중 어디로 가야 해?" | **Live** |
| "Arc4 살까 Arc9 살까?" | **Voyage** (trace 비교) |
| "이 항해 전체 연료 예산 얼마?" | **Voyage** (seek로 전구간 확인) |
| "실제 날씨 보며 수동 운항" | **Live** + 수동 조종 |
| "투자자·관계자한테 시연" | **Voyage** (스토리 + 이벤트) |
| "사후 사고 분석" | **Voyage** (로그 재생) |
| "신입 선장 교육" | **Voyage** (반복 재생) |

---

## Live Simulation 모드

### 진입
우측 사이드바 **Simulation Mode** → **Live Simulation** 클릭.

### 할 수 있는 것

#### 1) 자동 항해
좌측 사이드바에서 출발항·도착항 선택 → 상단 **Start** 버튼.
선박이 경로 따라 자동 이동. RIO·두께·이벤트 실시간 계산.

#### 2) 수동 조종
우측 사이드바 **수동 조종** 토글 ON. 조작:

| 키 | 기능 |
|---|---|
| **W** | 가속 (throttle +) |
| **S** | 감속 / 후진 |
| **A / D** | 좌/우 회전 |
| **X** | 급정지 |
| **Shift** | 부스트 |
| **B** | 쌍안경 줌 (FOV 15°) |

마우스:
- **드래그** — 카메라 오빗
- **휠** — 줌 인/아웃

#### 3) 텔레포트
좌측 상단 메뉴 → **Teleport** → lat/lon 입력 → 즉시 이동.
Three.js 선박 위치와 Cesium 카메라 동시 이동.

### Live 모드의 데이터 경로

```
실시간 WMS 해빙 격자 → sampleIceFn(lon,lat) → 해빙 농도 %
state.shipState (lat/lon/heading) → 3D 선박 위치
state.manualSpeed (W/S 키) → 속도
weather_latest.json → nearestWaveAt → updateShipMotion 파고/파향
```

### 주의

- **아라온은 Wrangel Island에서 움직이지 않음** (정박 마커만)
- **RIO는 hud.rfi 파싱** — 백엔드 자동 시뮬에서만 업데이트됨
- **진행률·누적 호위 · 출동 횟수** 같은 trace 전용 필드는 `—` 로 표시됨

---

## Voyage Playback 모드

### 진입
우측 사이드바 **Simulation Mode** → **Voyage Playback** 클릭.

→ 콘솔에 `[VoyageTrace] loaded nsr_month03_arc4.json: 519 ticks, 38 events` 뜨면 로드 완료 (~1초).

### 재생 컨트롤

우측 하단 **재생 컨트롤** 섹션:

| 요소 | 기능 |
|---|---|
| **Class 선택** (Arc4/Arc7/Arc9) | 선급별 trace 로드 |
| **▶ Play / ⏸ Pause** | 재생/정지 |
| **seek bar** | 시간 축 드래그 (직접 이동) |
| **Speed** | 1x / 10x / 60x / 300x 배율 |

### 속도별 실제 소요 시간

전체 519h(~21.6일) 항해를 재생하면:

| 속도 | 실시간 소요 |
|---|---|
| **1x** | 6시간 (디테일 관찰용) |
| **10x** | 36분 |
| **60x** | 약 9분 (기본 권장) |
| **300x** | 약 1분 40초 (빠른 훑기) |

**Tip**: 처음이면 **60x + seek bar 활용**. 디테일은 1x로.

### Arc4 항해 지도

| 시간 | 위치 | 얼음 두께 | 상태 |
|---|---|---|---|
| **t=0h** | 부산 출발 (35.1°N, 129°E) | 0 m | 개방 수역 |
| **t=50h** | 홋카이도 서해안 | 0 m | 개방 |
| **t=120h** | 오호츠크해 | 0 m | 개방 |
| **t=200h** | 베링해협 접근 | 0 m | 개방 |
| **t=234h** | 베링해협 통과 | 0 m 끝 | — |
| **t=235h** | 북극 첫 얼음 | **1.8 m** | 첫 조우 ⭐ |
| **t=260h** | 동시베리아해 초입 | 1~2 m | 얇은 얼음 |
| **t=300h** | 동시베리아해 중심 | 2~3 m | 두꺼움 |
| **t=322h** | **최대 두께 지점** | **3.72 m** | **피크 ⭐⭐** |
| **t=350h** | 랍테프해 | 2~3 m | 두꺼움 유지 |
| **t=400h** | 카라해 | 1~2 m | 얇아짐 |
| **t=450h** | 바렌츠해 | 0.5~1 m | 얇음 |
| **t=491h** | 바렌츠해 통과 | 0.06 m | 마지막 얼음 |
| **t=519h** | 노르웨이/북해 도착 | 0 m | 종료 |

**핵심 지점 (seek bar 위치)**:
- **북극 초입** → 약 45% (t≈235h)
- **얼음 피크** → 약 62% (t≈322h)
- **탈출 직전** → 약 93% (t≈480h)

### What-if 비교 예시 (Arc4 vs Arc9)

같은 t=300h를 두 선급으로 비교하면:

| 지표 | Arc4 | Arc9 |
|---|---|---|
| 얼음 두께 | 2.8 m | 2.8 m (동일) |
| 유효 두께 | 2.5 m (쇄빙선 도움) | 2.8 m (혼자 뚫음) |
| RIO | RESTRICT / BLOCKED | PASS |
| 아라온 status | 호위 중 | 대기 |
| 출동 횟수 (누적) | ~8 | ~2 |
| 저항 (kN) | 높음 | 보통 |
| 연비 (kg/h) | 악화 | 안정 |

**해석**: Arc4는 쇄빙선 지원 없이는 북극 피크 통과 어려움. Arc9는 자력 통과 가능. 투자·선급 선택 의사결정에 직접 연결.

---

## 카메라 뷰 (공통)

우측 사이드바 **View Mode** 또는 상단 CameraPanel에서 전환.

| 뷰 | 용도 | 특징 |
|---|---|---|
| **선미 추적** (FOLLOW) | 성능·거동 분석, 시연 | 3D 선박, 흔들림, 전방 프리뷰 HUD, 궤적 리본 |
| **위성 조감** (SATELLITE) | 의사결정 | Cesium top-down, 해빙 레이어·아라온·항로 |
| **광역 항로** (WIDE) | 전체 맥락 | 줌아웃된 전체 경로 |

### 선미 추적 인터랙션
- **마우스 드래그**: 카메라 오빗 (배 주변 회전)
- **마우스 휠**: 줌 인/아웃
- **B 키** (수동 모드): 쌍안경 줌

### 제거된 뷰
- ❌ **선교 1인칭** — 제거됨. 이유: 데이터가 2D 스칼라(두께·농도)라서 1인칭 시점에 정보 추가가 없고, 실제 선교엔 이미 창문이 있어 디지털 트윈의 가치(현장에서 못 보는 것)에 기여하지 못함. 선미추적으로 대체.

---

## UI 패널 설명

### 🧊 항로 상황 (VoyageInfoPanel — A 블록)

우측 상단. Voyage·Live 모두 표시 (이중 모드).

| 필드 | Voyage | Live |
|---|---|---|
| **t=X.Xh** | 재생 시간 | `실시간` |
| **얼음 두께** | trace `thickness_m` | — |
| **유효 두께** | `effective_thickness_m` | — |
| **해빙 농도** | — | 라이브 격자 (%) |
| **추정 두께** | — | 농도 proxy (`≈ X m`, 회색) |
| **속도** | 위치 diff로 유도 (kn) | `state.manualSpeed` or `hud.speed` |
| **RIO** | Polaris Ice 지수 + 배지 | `hud.rfi` 파싱 |
| **위치** | lat/lon | lat/lon |
| **진행률** | % · km | — |

**RIO 배지**:
- 🟢 `PASS` (≥0): 통과 OK
- 🟡 `CAUTION` (-3~0): 주의
- 🟠 `RESTRICT` (-6~-3): 제한적
- 🔴 `BLOCKED` (<-6): 통과 불가

### 🚢 아라온 (VoyageInfoPanel — B 블록)

| 필드 | Voyage | Live |
|---|---|---|
| **status 배지** | `대기`·`출동`·`접근`·`호위 중`·`해산` | `대기` (고정) |
| **호위 대상** | escorting_ship_id | — |
| **본선까지** | 실시간 계산 (km) | 실시간 계산 (km) |
| **누적 호위** | trace summary | — |
| **출동 횟수** | trace summary | — |
| **정박** | — | Wrangel Is. |

### ⚙ 성능 (추정 · Lindqvist) (VoyageInfoPanel — C 블록)

| 지표 | Voyage | Live |
|---|---|---|
| **쇄빙 저항 (kN)** | 과거 12h 라인차트 | 현재값 스냅샷 |
| **연료 소모율 (kg/h)** | 과거 12h 라인차트 | 현재값 스냅샷 |
| **배지** | `Lindqvist 기반` | `실시간 스냅샷` |

**⚠️ 추정값 주의**: 실제 센서 아니고 물리 근사치. 절대값 신뢰 ✕, 상대 추이 신뢰 ○.

### 🚢 전방 프리뷰 HUD (선미 추적 + Voyage 전용)

상단 중앙 오버레이.

- **16 bar 히스토그램** — 현재부터 전방 16 tick 얼음 두께
- **색상**: 파랑(<0.3m) → 청록(0.3~0.8) → 노랑(0.8~1.2) → 주황(1.2~1.8) → 빨강(>1.8m)
- **우측 배지**:
  - 🟢 `PASS`: 통과 가능
  - 🟡 `MARGINAL`: 주의
  - 🔴 `BLOCKED`: 통과 불가
- **hover**: 각 bar에 `+X km · Y.Ym · RIO Z` 툴팁

**역할**: "곧 뭐가 올지" 미리 보기. Live 모드에서는 안 뜸 (trace 기반 계산이라).

### 🌊 해역 기상 (WeatherHud)

우측 사이드바 중간. 현재 위치 기준 기상.

- **파고** (m) — Hs
- **가시거리** (km)
- **기온** (°C)
- **해수면 온도** (°C)
- **해무 위험** (자체 계산)

### 📍 현재 위치 미니맵

우측 사이드바 하단. 극지 top-down 미니맵. 선박 궤적·아라온·경로 표시.

### 🎬 VoyageHUD (기존)

좌측 하단 작은 박스. Voyage 재생 중에만 표시.

- `Time t=X.Xh / 519h`
- `Class Arc4 @ 15.0kn`
- `RIO X.XX` (색상 tint)
- `Max RIO hit X`
- `Calls X / intercept_failed X`
- `Escorted X.X km`

### 🚨 VoyageEventToast

이벤트 발생 시 좌측 상단에 3초 토스트:

```
[ESCORT] t=245.0h 아라온
[CALL]   t=262.0h 아라온
[FAIL]   t=290.0h 아라온
```

---

## 선박 거동 (흔들림) 시스템

### 1. 파도 기반 roll/pitch (real wave override)

- **소스**: `weather_latest.json` 의 가장 가까운 waypoint 파고·파향·주기
- **로직**: 선박 heading 대비 파향 상대각으로 축 분리
  - **횡파 (beam sea)**: 파가 옆구리로 올 때 → **roll 우세** (좌우 기우뚱)
  - **종파 (head sea)**: 파가 정면으로 올 때 → **pitch 우세** (뱃머리 상하)
  - **경사파 (quarter sea)**: 대각선 → roll·pitch 반반
- **진폭**: Hs 커질수록 커짐
- **공진**: 파주기 Tp가 선박 고유 주기(~8s)에 가까울수록 증폭
- **fallback**: 파향 데이터 없으면 스칼라 magnitude만. 근접 waypoint 없으면 위도 기반 합성(`getSeaState(lat)`).

### 2. 얼음 기반 bow-up pitch (Voyage만)

- 유효 두께 × 속도 → bow-up 편향
- 두꺼운 얼음 뚫을 때 뱃머리 1~8° 들림
- RIO 심할 때 작은 roll 편향 추가

### 3. 궤적 리본 (선미뷰 전용)

- 배가 지나간 궤적을 cyan 라인으로 렌더
- 240 포인트 ring buffer (약 30초 분량)
- 꼬리로 갈수록 어두워짐
- 다른 뷰 전환 시 사라지고, 선미뷰 재진입 시 새 궤적

### 눈으로 확인하는 법

- **선미 추적 뷰**에서 **수평선 위치** 주시
- Voyage t=320h 근처로 seek → 수평선이 화면 아래로 살짝 내려가면 bow-up pitch 작동 (얼음 피크 진입 중)
- 바렌츠해 개방 수역(t=450h~)에서는 파도 Hs 크니까 **roll 폭이 눈에 띄게 증가**

---

## 이벤트 & 자동 전환

### 이벤트 종류 (Voyage 전용)

| 태그 | 의미 | 색 |
|---|---|---|
| `[CALL]` | 본선이 아라온 호출 | 노랑 |
| `[RDV]` | 아라온 본선 접근 | 주황 |
| `[ESCORT]` | 아라온 호위 시작 | 빨강 |
| `[RELEASE]` | 호위 해제 | 파랑 |
| `[RETURN]` | 아라온 복귀 | 회색 |
| `[FAIL]` | 아라온 제때 도착 실패 | 진빨강 |
| `[ARRIVE]` | 본선 목적지 도착 | 녹색 |

### VoyageAutoCam (자동 전환)

**주요 이벤트** 발생 시 자동으로 **선미 추적 뷰 전환 → 5초 후 복귀**.

- 트리거: `start_escort`, `rendezvous`, `intercept_failed`, `arrive`
- 복귀: 저장된 이전 카메라 모드로
- 취소: 전환 중 사용자가 직접 모드 변경 시 자동 복귀 취소 (사용자 우선)

---

## 빠른 워크플로우 예시

### 예시 A — Voyage 첫 체험 (5분)

1. **Voyage Playback** 버튼 → trace 로드 대기
2. 카메라 **선미 추적** 으로 전환
3. Speed **60x** (기본)
4. **▶ Play**
5. seek bar **60%** 로 드래그 (t=310h)
6. 우측 상단 VoyageInfoPanel 관찰:
   - 얼음 두께 ~3m
   - RIO 주황/빨강
   - 아라온 status `호위 중`
   - 저항·연비 라인차트 피크
7. 선박 흔들림·수평선 기울임 관찰
8. 전방 프리뷰 HUD의 BLOCKED/MARGINAL 배지 확인

### 예시 B — 이벤트 순간 집중 관찰

1. Voyage 재생 중 seek bar 45% (t≈235h, 북극 진입)
2. Speed **10x** (천천히)
3. ▶ Play
4. VoyageEventToast에 `[CALL]`·`[ESCORT]` 뜨는 순간 자동으로 선미 뷰 전환
5. 5초 후 원래 뷰로 복귀

### 예시 C — 선급 비교 (Arc4 vs Arc9)

1. Arc4 로드 → seek 60% → VoyageInfoPanel 값 메모
2. Ice Class 선택을 **Arc9** 로 바꿈 → 자동 리로드
3. 다시 seek 60%
4. 두 값 비교:
   - Arc9는 아라온 호출 거의 없음
   - RIO 거의 PASS
   - 저항 낮음
5. **결론**: 선급 투자 ROI 시각적으로 즉시 도출

### 예시 D — Live 수동 운항

1. **Live Simulation** 버튼
2. 카메라 **선미 추적**
3. 우측 사이드바 **수동 조종** 토글 ON
4. **W** 가속
5. **A/D** 회전 + 라이브 해빙 농도 피해가며 이동
6. VoyageInfoPanel 에 해빙 농도, 속도, 위치 실시간 업데이트 확인

---

## 자주 묻는 질문 (FAQ)

### Q1. 왜 얼음 두께가 0으로 뜨지?
- **Live**: 저위도 해역 (대한해협·동해) 은 해빙 없음 → 정상
- **Voyage t=0~234h**: 부산→베링해 개방 수역 → 정상
- **Voyage t=235~491h**: 얼음 구간 → 0이 아니어야 함

### Q2. Voyage에서 텔레포트가 안 먹어
**재생 데이터가 선박 위치를 덮어써**. Voyage에선 텔레포트 무의미. seek bar로 시간 이동.

### Q3. 수동 조종 키가 안 먹어 (Voyage)
같은 이유. Voyage는 재생 모드라 조작 불가. Live로 전환.

### Q4. 60x 속도인데 UI가 느려
60x → 1 sim hour = 60 real sec. `t=X.Xh` 1 decimal 표시는 6초마다 한 번 증가. 정상이야. 300x로 올리면 빨라짐.

### Q5. 저항·연비 숫자 믿을 수 있어?
**추정값**이야. Lindqvist 공식 기반 근사. 절대값 신뢰 ✕, 상대 추이 신뢰 ○. 백엔드에 실측 필드 생기면 교체 예정.

### Q6. Live 모드에서 RIO가 0에 멈춰있어
`hud.rfi`는 **자동 시뮬 루프에서만 업데이트**됨. 수동 조종 중에는 정지. 의도된 동작이야 (백엔드 RIO 계산 모델 필요).

### Q7. Arc9는 왜 이벤트가 Arc4보다 많지?
Arc9는 강한 내빙선이라 **자체로 뚫고 지나감** → 아라온 호출 없음 → 대신 **진입/탈출·RIO 변화** 이벤트가 더 촘촘히 기록됨. 숫자 ≠ 위험도.

### Q8. 전방 프리뷰 HUD가 Live에서 안 뜨는데?
**Voyage 전용**. 이유: trace의 미래 tick 을 보고 만드는 거라 Live엔 "미래 데이터"가 없음. Live에서 구현하려면 현재 heading 기준 전방 좌표 N개 만들어 라이브 격자 샘플링해야 하는데, 정확도 떨어지고 시각적 트릭이 되기 쉬워서 의도적으로 제외.

### Q9. 내가 만든 시뮬 데이터 재생 가능?
백엔드 `simulate_voyage` trace JSON 포맷 따르면 OK. `Digital_twin/frontend/public/simulations/` 에 파일 넣고 로더 코드에 파일명 등록.

### Q10. 선교 1인칭 뷰 어디 갔어?
**제거됨**. 이유:
- 2D 스칼라 데이터(두께·농도)라 1인칭에 정보가 없음
- 실제 선교엔 이미 창문 있어 디지털 트윈의 가치에 기여 못 함
- 선미 추적이 성능 분석·시연 모두 더 적합

나중에 ridge DEM, floe 경계 같은 3D 기하 데이터가 들어오면 부활 고려.

---

## 한계와 주의사항

1. **Voyage 모드 = 녹화 재생**. 조작 불가는 정상.
2. **60x 속도 + 1 decimal 표시**: 초반엔 "멈춘 것처럼" 보임. 인내심 + seek bar.
3. **얼음 두께 0m**: 항로 초반/후반은 개방 수역이라 당연. t=235~491 사이가 유의미.
4. **저항·연비 숫자**: Lindqvist 근사. 절대값 신뢰 ✕.
5. **Live 모드 아라온**: Wrangel Is. 정박 고정. 동행·호위 안 함.
6. **Live 모드 RIO**: 자동 시뮬 루프에서만 업데이트. 수동 조종 중 정지.
7. **파향 없는 해역**: 파도 흔들림 약해짐 (축 분리 불가 → 스칼라 fallback).
8. **파고 데이터 커버리지**: weather_latest.json 의 route waypoint 만 있음. 경로 밖 해역은 1500km 이내 최근접 fallback, 없으면 위도 합성.
9. **Voyage + 수동 조종**: Voyage 재생 중 수동 조종 토글은 **무효**. 재생이 선박 위치를 매 tick 덮어씀.

---

## 개발자 참조

### 주요 파일

| 경로 | 내용 |
|---|---|
| `frontend/src/App.jsx` | 모드 전환, 시뮬 루프, VoyageInfoPanel 렌더 |
| `frontend/src/hooks/useVoyagePlayback.js` | Voyage RAF 재생 루프 |
| `frontend/src/services/voyageTrace.js` | trace 로드·보간 |
| `frontend/src/services/derivedMetrics.js` | 속도·저항·연비·프리뷰·파 lookup 유도식 |
| `frontend/src/services/cameraManager.js` | 카메라 모드 전환, FOLLOW 카메라 로직 |
| `frontend/src/components/ThreeOverlay.jsx` | 3D 씬, 선박 거동, 궤적 리본, FOLLOW 렌더 루프 |
| `frontend/src/components/VoyagePlayback/VoyagePlaybackLayer.jsx` | Cesium entity 생성/갱신 |
| `frontend/src/components/VoyagePlayback/VoyageAutoCam.jsx` | 이벤트 자동 전환 디렉터 |
| `frontend/src/components/hud/VoyageInfoPanel.jsx` | 이중 모드 상황·아라온·성능 패널 |
| `frontend/src/components/hud/ForwardPreviewHUD.jsx` | 전방 프리뷰 히스토그램 |
| `frontend/src/components/hud/CameraPanel.jsx` | 카메라 모드 버튼 |
| `frontend/src/context/AppContext.jsx` | 글로벌 state (shipState, hud, mode, manual) |
| `backend/pipeline/fetchers/weather_fetcher.py` | Open-Meteo Marine API → wave_height/direction/period |
| `backend/pipeline/fetchers/copernicus_wave_fallback.py` | Copernicus VHM0/VMDR/VTM02 fallback |
| `backend/data/simulations/nsr_month03_*.json` | trace 원본 |
| `frontend/public/simulations/` | 프론트 서빙 위치 |

### 데이터 포맷

#### Voyage trace JSON
```json
{
  "metadata": {
    "route": "NSR",
    "ship": { "id": "...", "ice_class": "Arc4", "speed_knots": 15.0 },
    "month": 3,
    "dt_hours": 1.0,
    "total_ticks": 519,
    "duration_hours": 519.0
  },
  "ticks": [
    {
      "t": 0.0,
      "ship": {
        "position": { "lat": 35.1, "lon": 129.04 },
        "rio": 2.0,
        "thickness_m": 0.0,
        "effective_thickness_m": 0.0,
        "km_along_route": 0.0
      },
      "icebreakers": [
        {
          "id": "ib-araon",
          "position": { "lat": 71.0, "lon": 179.5 },
          "status": "idle",
          "escorting_ship_id": null
        }
      ],
      "events": []
    }
  ],
  "summary": {
    "icebreaker_calls": 8,
    "intercept_failed": 0,
    "total_escort_distance_km": 340.0,
    "max_rio_violation": -6,
    "completed": true,
    "total_route_km": 14380.0
  }
}
```

#### weather_latest.json (파고 필드)
```json
{
  "routes": {
    "NSR": {
      "waypoints": [
        {
          "name": "...",
          "lat": 72.0,
          "lon": 140.0,
          "wave_height_m": 1.2,
          "wave_direction_deg": 185.0,
          "wave_period_s": 7.4,
          "temperature_c": -5.3,
          "visibility_km": 12.0
        }
      ]
    }
  }
}
```

### derivedMetrics API

```js
import {
  deriveSpeedKn,          // (trace, tHours) → knots
  deriveHeadingDeg,       // (trace, tHours) → degrees
  deriveMotion,           // ({Hs, dir, period, heading, speed, thickness}) → {rollRad, pitchRad, heaveM}
  deriveIceResistanceKN,  // ({effectiveThicknessM, speedKn, beamM}) → kN
  deriveFuelRateKgH,      // ({resistanceKN, speedKn}) → kg/h
  deriveForwardPreview,   // (trace, tHours, nTicks) → [{t, kmAhead, thickness, rio}]
  derivePassBadge,        // (preview) → {level, label, color}
  deriveResistanceSeries, // (trace, tHours, windowH, specs) → [{t, resistanceKN, fuelKgH, ...}]
  nearestWaveAt,          // (weatherData, lat, lon, maxKm) → {height, direction, period} | null
} from './services/derivedMetrics';
```

### ThreeOverlay ref API (선박 거동 제어)

```js
const three = threeRef.current;
three.setVoyageMotionBias({ rollRad, pitchRad, heaveM });  // 얼음 기반 bias 주입
three.setVoyageMotionBias(null);                            // 해제
three.setRealWaveInput({ Hs, Tp, dirDeg, headingDeg });    // real wave override
three.setRealWaveInput(null);                               // 해제 (위도 합성 fallback)
three.updateShipMotion(dt, lat);                            // 수동 호출 (보통 render loop가 알아서)
```

---

## 핵심 한 문장

> **Live = 실시간 의사결정 · Voyage = 분석·검증·시연**
>
> 둘을 같이 쓸 때 비로소 "현장에서 못 보는 것"을 보여주는 진짜 디지털 트윈이 완성돼.
