# Arctic Digital Twin — AI 모델 설계 및 개발 결과 보고서

> **문서 목적** · AI 개발 프로세스 **3단계 「모델 설계 및 개발」**의 결과 산출물
> **대상 시스템** · Arctic Digital Twin (북극항로 통항 AI 의사결정 플랫폼)
> **범위** · 데이터 → 모델 → 학습 → 결과의 전 과정을 5종 AI 구성요소 단위로 정리

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [AI 개발 프로세스 매핑](#2-ai-개발-프로세스-매핑)
3. [전체 AI 아키텍처](#3-전체-ai-아키텍처)
4. [데이터 설계](#4-데이터-설계)
5. [모델 설계](#5-모델-설계)
6. [학습 프로세스](#6-학습-프로세스)
7. [평가 지표 및 결과](#7-평가-지표-및-결과)
8. [배포 및 운영](#8-배포-및-운영)
9. [부록 — 주요 파일 인덱스](#9-부록--주요-파일-인덱스)

---

## 1. 프로젝트 개요

### 1.1 문제 정의

북극항로(Northern Sea Route)는 전통적인 수에즈 항로 대비 항해 거리·시간을 크게 단축할 수 있으나, **해빙·빙산·기상**이라는 고위험 변수가 상존한다. 본 프로젝트는 위성·해양 데이터를 AI로 분석하여 **"언제 출항할지, 어떤 경로로 갈지, 빙산을 어떻게 회피할지, 비용이 얼마나 드는지"** 를 정량적으로 의사결정하는 디지털 트윈을 구축한다.

### 1.2 해결 접근 — AI 5종 통합

| # | 의사결정 질문 | 모델 | 알고리즘 | 산출물 |
|---|---|---|---|---|
| **(C) 빙산 회피** | "지금 어디로 키를 틀어야 하나?" | 강화학습 | SAC (Stable-Baselines3) | 실시간 침로·속력 보정 |
| **(A) 출항 스케줄링** | "언제 떠나는 것이 안전한가?" | 강화학습 | SAC (Stable-Baselines3) | 날짜별 출항 적합도 |
| **연료 예측** | "북극항로가 수에즈보다 경제적인가?" | 회귀 | XGBoost | 해리당 연료 + 비용 비교 |
| **빙산 탐지** | "위성 영상 속 빙산은 어디 있나?" | 컴퓨터 비전 | YOLOv8n | 빙산 bbox·신뢰도 |
| **What-If 분석** | "다른 시나리오는 어떤 결과인가?" | LLM 에이전트 | Claude Agent SDK | 6~8개 대안 시나리오 |

> 출처: [README.md](../README.md) · 본 보고서는 README가 다루지 않는 **설계 근거·입출력 스펙·결과 스키마**를 보강한다.

---

## 2. AI 개발 프로세스 매핑

본 프로젝트는 표준 AI 개발 6단계를 따르며, 본 문서는 **3단계(모델 설계)** 를 중심으로 4·5·6단계 결과 일부를 포함한다.

| 단계 | 프로세스 | 본 프로젝트 적용 | 본 문서 범위 |
|---|---|---|---|
| 1단계 | 문제 정의 및 기획 | 북극항로 통항 위험·경제성 의사결정 | §1 (요약) |
| 2단계 | 데이터 수집 및 전처리 | Sentinel-1 / NSIDC / Copernicus 수집 | §4 |
| **3단계** | **모델 설계 및 개발** | **SAC·XGBoost·YOLOv8·LLM 5종 설계** | **§5 (핵심)** |
| 4단계 | 학습 및 최적화 | 커리큘럼·반복·전이 학습 | §6 |
| 5단계 | 평가 및 검증 | 성공률/충돌률/mAP/R² | §7 |
| 6단계 | 배포 및 모니터링 | HF Spaces·워치독 | §8 |

---

## 3. 전체 AI 아키텍처

5종 모델은 각각 독립 마이크로서비스로 분리되어, 한 서비스의 부하(예: SAR 학습)가 다른 서비스(실시간 RL 추론)를 막지 않도록 설계되었다.

```
                          ┌──────────────────────┐
                          │   사용자 / 프론트엔드   │
                          │ React + Cesium 3D Map │
                          │   (Vercel 호스팅)      │
                          └───────────┬──────────┘
                                      │ HTTPS  /api/*
                                      ▼
        ┌─────────────────────────────────────────────────────────┐
        │       AWS EC2 (Docker, t2.medium · 2vCPU/4GB)           │
        │  ┌───────────────────────────────────────────────────┐  │
        │  │           Node 게이트웨이  (port 8000)             │  │
        │  └──┬─────────────┬──────────────┬──────────────┬────┘  │
        │     │             │              │              │       │
        │  ┌──▼───────┐ ┌───▼────────┐ ┌───▼───────┐ ┌────▼─────┐ │
        │  │rl-pipeline│ │report-     │ │ml-pipeline│ │sar-server│ │
        │  │  :8001    │ │service     │ │  :8003    │ │  :8005   │ │
        │  │           │ │  :8002     │ │           │ │          │ │
        │  │  SAC      │ │ SAC(출항)  │ │ XGBoost   │ │ YOLOv8n  │ │
        │  │ (빙산회피) │ │ +Claude SDK│ │ (연료)    │ │ (빙산탐지)│ │
        │  │           │ │ +리포트생성 │ │           │ │          │ │
        │  └──────────┘ └────────────┘ └───────────┘ └──────────┘ │
        └────────────────────────────┬────────────────────────────┘
                                      │
                                      ▼
        ┌─────────────────────────────────────────────────────────┐
        │                    외부 데이터 소스                        │
        │  Copernicus Marine (해양) · CDSE Sentinel-1 (SAR)        │
        │  NSIDC (해빙 농도) · Anthropic Claude API (시나리오)      │
        └─────────────────────────────────────────────────────────┘
```

**포트 매핑**

| 서비스 | 포트 | 책임 | 진입점 |
|---|---|---|---|
| Proxy | 7860 | 라우팅 게이트웨이 | (HF Spaces) |
| rl-pipeline | 8001 | 빙산 회피 RL 추론/학습 | [backend/services/rl-pipeline/server.py](../backend/services/rl-pipeline/server.py) |
| report-service | 8002 | 출항 RL + What-If + 리포트 | [backend/services/report-service/server.py](../backend/services/report-service/server.py) |
| ml-pipeline | 8003 | 연료 예측 회귀 | [backend/services/ml-pipeline/server.py](../backend/services/ml-pipeline/server.py) |
| sar-server | 8005 | 빙산 탐지 학습 | [sar_server.py](../sar_server.py) |

> 출처: [README.md:44-79](../README.md) · 각 서버 모듈 docstring

---

## 4. 데이터 설계

### 4.1 데이터 소스

| 데이터 | 출처 | 형태 | 용도 |
|---|---|---|---|
| SAR 위성 영상 | Copernicus CDSE (Sentinel-1) | 2채널 편파(VV·VH) 래스터 | 빙산 탐지 입력 |
| 해빙 농도 | NSIDC (NASA) | 일별 격자(0~1) | 회피·출항·항로 스코어링 |
| 해양·기상 | Copernicus Marine | 수온·해류·파고 격자 | 출항 관측·연료 보정 |
| 빙산 위치 | Iceberg 리포트 | 점(위경도)+크기 | 회피 환경 구성 |

> 수집기: [backend/pipeline/fetchers/](../backend/pipeline/fetchers) (`sentinel1_iw_fetcher.py`, `nsidc_pipeline.py`, `copernicus_fetcher.py`, `iceberg_fetcher.py`)

### 4.2 데이터 형태별 정리

**① 해빙 차트 (POLARIS 입력)** — 농도(0~1)를 WMO 빙종으로 근사 변환

| 농도 구간 | 대표 빙종 매핑 |
|---|---|
| < 0.05 | Open Water |
| 0.05 ~ 0.15 | Open Water + Grey Ice |
| 0.15 ~ 0.40 | Open Water + Grey-White Ice |
| 0.40 ~ 0.70 | Open Water + Thin First-Year |
| 0.70 ~ 0.85 | Thin + Medium First-Year |
| ≥ 0.85 | Medium + Thick First-Year |

> 구현: [route_scorer.py:72-105](../backend/services/report-service/modules/route_scorer.py#L72-L105) `concentration_to_ice_conditions()`

**② SAR 이미지** — 3채널 합성 (VV, VH, VV/VH 비율), 640×640 리사이즈, YOLO 포맷 라벨

**③ 강화학습 관측 벡터**

| 모델 | 관측 차원 | 주요 구성 |
|---|---|---|
| 빙산 회피 | **22-dim** | 위경도·침로·속력·웨이포인트 거리/방위·빙산 상대좌표·가시거리·파고·항로이탈 |
| 출항 스케줄링 | **28-dim** | 월별 해빙 통계·기상·캘린더(잔여일)·선박 속성 |

> 회피 관측 정규화 스펙: [model/avoidance-route/onnx/NSR_normal_normalization.json](../model/avoidance-route/onnx/NSR_normal_normalization.json)

**④ 항로 세그먼트** — POLARIS 스코어링 단위

| 항로 | 세그먼트 수 | 예시 구간 |
|---|---|---|
| NSR | 7 | 베링해협·척치해·동시베리아해·랍테프해·빌키츠키해협·카라해·바렌츠해 |
| NWP | 5 | 보퍼트해·앰마누엘반도·랭커스터해협·배핀만·데이비스해협 |
| TSR | 3 | 북극해중앙·그린란드해·노르웨이해 |

> 정의: [route_scorer.py:43-65](../backend/services/report-service/modules/route_scorer.py#L43-L65) `ARCTIC_SEGMENTS`

### 4.3 전처리 파이프라인

```
외부 API ──▶ fetchers ──▶ 격자/래스터 정규화 ──┬──▶ POLARIS RIO 변환 (calculate_rio)
                                              │      └─ 항로별·날짜별 위험 점수
                                              ├──▶ RL 관측 벡터 정규화 (22/28-dim)
                                              └──▶ SAR 3채널 합성 + YOLO 라벨
```

> POLARIS RIO 계산: `arctic_master_router.calculate_rio()` 재사용 ([route_scorer.py:37](../backend/services/report-service/modules/route_scorer.py#L37))

---

## 5. 모델 설계

> 각 모델은 **목적 → 입출력 → 구조/하이퍼파라미터 → 저장 형식 → 결과 예시** 순으로 기술한다.

### 5.1 빙산 회피 — SAC 강화학습 (rl-pipeline)

**목적** · 선박 상태와 주변 빙산·기상을 입력받아 충돌을 회피하는 실시간 침로·속력 보정을 생성.

**입력 / 출력**

| 구분 | 스펙 |
|---|---|
| 관측 (22-dim) | 위경도, 침로, 속력, 웨이포인트 거리/방위, 빙산 상대 위치, 가시거리, 파고, 항로 이탈량 |
| 액션 (2-dim) | `heading_delta` ∈ [−15°, +15°], `speed_factor` ∈ [0.5, 1.0] |

> 입력 API 스키마: `ship_state{lon,lat,heading,speed_knots,ice_class,progress}`, `icebergs[]`, `ice_data{concentration}`, `weather{visibility_km,wave_height_m}` ([rl-pipeline/server.py:64-68](../backend/services/rl-pipeline/server.py#L64-L68))

**모델 매트릭스** · 배포본은 **커리큘럼 9종**, 학습 파이프라인은 **선박별 확장 84종**을 지원한다.

```
[배포 ONNX 9종 = 항로 3 × 난이도 3]      [멀티모델 확장 84종 = 항로 3 × 빙급 7 × 선종 4]
          easy   normal  hard                       bulk container  lng  tanker
   NSR  ┌──────┬───────┬──────┐            IA Super │                          │
   NWP  │  ✔   │   ✔   │  ✔   │  × NSR/NWP  IA       │   RLMultiModelTrainer    │
   TSR  └──────┴───────┴──────┘    /TSR     PC3..PC7 │   (server.py 통합)        │
```

> 배포 9종: [model/avoidance-route/onnx/](../model/avoidance-route/onnx) (`{ROUTE}_{easy|normal|hard}.onnx.data` + `_normalization.json`)
> 확장 84종 설계: `RLMultiModelTrainer`(`ALL_COMBINATIONS = ROUTES × ICE_CLASSES × SHIP_TYPES`) — [rl-pipeline/server.py:38](../backend/services/rl-pipeline/server.py#L38)

**하이퍼파라미터** (SAC)

| 항목 | 값 | 설계 의도 |
|---|---|---|
| learning_rate | 3e-4 | 수렴 속도 확보 |
| buffer_size | 500,000 | 메모리 절약 + 최근 경험 비중↑ |
| batch_size | 256 | 더 잦은 업데이트 |
| gamma | 0.99 | 단기 보상(진행도) 중시 |
| tau / ent_coef | 0.005 / auto | 타깃 부드러운 갱신 / 자동 엔트로피 |
| learning_starts | 5,000 | 조기 성공 경험 축적 |
| net_arch | [256, 256] | 표준 크기(과적합 방지) |

> 출처: [rl_agent.py:68-81](../backend/services/rl-pipeline/modules/rl_agent.py#L68-L81)

**보상 설계** · `collision`(충돌 페널티) · `success`(목표 도달) · `progress`(전진) · `efficiency`(연료/이탈) 4종 가중 (`RewardWeights`). 반복 학습 시 목표 미달이면 가중치를 자동 재조정.

**저장 형식** · 배포 = ONNX(중앙 `model/avoidance-route/onnx/`, 외부 가중치 `.onnx.data` + 정규화 JSON) / 개발·백업 = SB3 ZIP(`rl-pipeline/models/sac_iceberg/sac_v1~v3.zip`).

**결과 예시**

```json
{
  "recommended_heading": 45.3,
  "recommended_speed_knots": 8.2,
  "heading_delta": 7.5,
  "speed_factor": 0.82,
  "collision_risk": 0.02,
  "path_preview": [{"lon": -170.5, "lat": 72.3, "heading": 45.3}]
}
```

---

### 5.2 출항 스케줄링 — SAC 강화학습 (report-service)

**목적** · 월별 해빙·기상 통계와 캘린더를 입력받아 **날짜별 출항 적합도**를 산출.

**입력 / 출력**

| 구분 | 스펙 |
|---|---|
| 관측 (28-dim) | 월별 해빙 통계 + 기상 + 캘린더(잔여일) + 선박 속성 |
| 액션 (1-dim) | 출항 적합도 confidence ∈ [−1, +1] (양수=권장, 음수=비권장) |

**모델 매트릭스** · **29종 = 빙급 7 × 선종 4 + 기본 1**

```
            bulk    container   lng    tanker        + default (PC5, departure_sac.onnx)
 IA Super  ┌─────┬──────────┬──────┬────────┐
 IA        │                                │
 PC3       │   (ice_class)_(ship_type)      │   파일 키: "{ice_class}_{ship_type}"
 PC4       │      .onnx 모델 1개씩           │   예) PC5_container → departure_sac_PC5_container.onnx
 PC5       │                                │
 PC6       │                                │
 PC7       └─────┴──────────┴──────┴────────┘
```

> 빙급/선종 목록 및 검증: [services-launcher/test_departure_models.py:15-16](../services-launcher/test_departure_models.py#L15-L16)
> 모델 키 규칙·로드 우선순위: [departure_agent.py:30-60](../backend/services/report-service/modules/rl/departure_agent.py#L30-L60)

**하이퍼파라미터** (SAC) · learning_rate 3e-4 · buffer_size 50,000 · batch_size 256 · gamma 0.99 · tau 0.005 · 학습 timesteps 100,000 ([departure_agent.py:112-143](../backend/services/report-service/modules/rl/departure_agent.py#L112-L143))

**저장 형식** · 배포 = ONNX(`model/report-service/*.onnx`, onnxruntime 추론) 우선 → 미존재 시 SB3 ZIP(`data/departure_rl_model/*.zip`) 폴백. 학습 중 `CheckpointCallback`이 5,000 스텝마다 체크포인트 저장, 최근 3개만 유지.

**결과 예시**

```json
{
  "ice_class": "PC5", "ship_type": "container", "model_format": "onnx",
  "departure_scores": {
    "2026-06-01": 0.92,
    "2026-06-02": 0.45,
    "2026-06-03": -0.15
  }
}
```

---

### 5.3 연료 소비 — XGBoost 회귀 (ml-pipeline)

**목적** · 빙해 저항을 반영한 **해리당 연료 소비량**을 예측하고, 이를 기반으로 NSR vs 수에즈 **경제성**을 비교.

**입력 / 출력**

| 입력 feature (6) | 의미 | 출력 |
|---|---|---|
| `displacement` | 배수량 (tons) | `fuel_per_nm` (tons/해리) |
| `draft` | 흘수 (m) | — |
| `engine_power` | 엔진 출력 (kW) | log-transform 학습 후 |
| `ice_thickness` | 빙하 두께 (0~3 m) | `np.exp()` 역변환 |
| `ice_concentration` | 빙하 농도 (0~1) | |
| `ice_class_code` | 내빙등급 코드 (0/2/4) | |

> 입력 모델·예측 로직: [ml-pipeline/server.py:58-154](../backend/services/ml-pipeline/server.py#L58-L154)

**경제성 비교 비용 상수**

| 항목 | 컨테이너 | LNG | 쇄빙선 |
|---|---|---|---|
| 수에즈 통행료 (USD) | 300,000 | 450,000 | 250,000 |
| 쇄빙 에스코트 (USD/일) | 85,000 | 120,000 | 0 |
| 북극 보험 (USD/일) | 15,000 | 45,000 | 8,000 |
| 수에즈 보안비 (USD) | 20,000 | 35,000 | 15,000 |

> 연료 단가 VLSFO 600 USD/ton. 출처: [ml-pipeline/server.py:83-114](../backend/services/ml-pipeline/server.py#L83-L114)

**저장 형식** · `model/nevigation-service/fuel_xgb_model.pkl` (joblib, `{model, metrics, log_transformed}` 딕셔너리).

**엔드포인트** · `/api/fuel/predict` (단일 구간 예측), `/api/fuel/compare` (NSR↔수에즈 비교).

**결과 예시**

```json
{
  "nsr":  {"total_fuel_tons": 145.6, "transit_days": 20.5, "total_cost_usd": 1187360},
  "suez": {"total_fuel_tons": 162.3, "transit_days": 38.0, "total_cost_usd": 1397000},
  "comparison": {"cost_saving_usd": 209640, "time_saving_days": 17.5}
}
```

---

### 5.4 빙산 탐지 — YOLOv8n (sar-server)

**목적** · Sentinel-1 SAR 영상에서 빙산을 탐지·분류.

**입력 / 출력** · 입력 = 3채널 SAR(VV·VH·VV/VH) 640×640 / 출력 = 빙산 bbox + 클래스 + 신뢰도.

**모델 구조** · YOLOv8n(COCO 사전학습) 전이학습.

**학습 설정**

| 항목 | 기본값(CLI) | API 기본값 |
|---|---|---|
| epochs | 50 | 30 |
| batch_size | 8 | 4 |
| img_size | 640 | 640 |
| device | cpu | cpu |
| resume | 자동 감지(미완료 시 재개) | — |
| 증강 | — | flipud 0.5, fliplr 0.5, degrees 180, scale 0.3, mosaic 0.5, hsv_v 0.3 |

> 학습기: [iceberg_model_trainer.py:38-90](../backend/pipeline/trainers/iceberg_model_trainer.py#L38-L90) · 데이터셋: `backend/data/datasets/sar_icebergs/data.yaml` (YOLO 포맷)

**반복 학습** · `IcebergIterativeTrainer`가 1회 학습 후 메트릭을 평가, 목표 미달 시 합성 데이터(`synthetic_count=200`)를 증강하며 `max_iterations=3`까지 자동 반복 ([sar_server.py:66-118](../sar_server.py#L66-L118)).

**결과 예시**

```json
{
  "is_training": false, "mode": "iterative", "iteration": 3,
  "metrics": {"mAP50": 0.87, "mAP50_95": 0.64, "precision": 0.89, "recall": 0.82},
  "detections": [{"x": 450, "y": 380, "w": 120, "h": 95, "confidence": 0.96}]
}
```

---

### 5.5 What-If 시나리오 — Claude Agent SDK (report-service)

**목적** · 출항 결정을 둘러싼 **대안 시나리오 6~8개**를 LLM으로 자동 생성하고 POLARIS RIO 기준으로 추천/비추천을 분류.

**구성 정책**

| 요소 | 값 | 설명 |
|---|---|---|
| Claude 핵심 시나리오 | 최대 3개 (`CLAUDE_SCENARIO_CAP`) | 기준/추천/비추천 |
| 풀 보강 | 3~5개 | 하드코딩 풀에서 무작위 추출 |
| 총 시나리오 | 6~8개 (`MIN/MAX_TOTAL_SCENARIOS`) | 매 호출 보장 |
| 중복 제거 | 5축 키 | route·ice_class·total_days·avg_rio·ice_multiplier |

**추천 분류 규칙** (`classify_recommendation_rio`)

| 조건 | 판정 |
|---|---|
| `avg_rio` ≥ 1.5 **그리고** `safe_passage_pct` ≥ 50% | 추천 |
| `avg_rio` ≥ 1.5 이지만 `safe_passage_pct` < 50% | 조건부 |
| 0 ≤ `avg_rio` < 1.5 | 조건부 |
| `avg_rio` < 0 (Elevated Operations) | 비추천 |

> 출처: [whatif_generator_max.py:53-81](../backend/services/report-service/modules/whatif_generator_max.py#L53-L81)

**도구(MCP) 통합** · Claude Agent SDK가 `WhatIfToolExecutor`(route_scorer 등)를 도구 호출로 사용하여 실제 POLARIS 점수를 시나리오의 single source of truth로 활용(텍스트 파싱 비의존).

**인증/실행 요구** · `claude-agent-sdk` 패키지 + Claude Code CLI + Max 플랜 OAuth 로그인.

---

## 6. 학습 프로세스

| 모델 | 학습 방식 | 핵심 메커니즘 |
|---|---|---|
| 회피 SAC | 단일 / 커리큘럼 / 반복 멀티모델 | easy→normal→hard 단계 상승, 목표 미달 시 보상 자동 재조정 |
| 출항 SAC | 단일 / 반복 | 30일 예측 윈도우, 체크포인트 5K 스텝 |
| 연료 XGBoost | 정적 배치 | log-target 회귀 (안정성) |
| YOLOv8 | 전이학습 + 반복 | last.pt 자동 재개, 합성 데이터 증강 |

**RL 학습 목표치** · 반복 학습기는 `target_success_rate=0.70`, `target_collision_rate=0.15`를 기준으로 수렴 여부를 판정 ([rl-pipeline/server.py:77-92](../backend/services/rl-pipeline/server.py#L77-L92)).

**학습 모니터링** · [continuous_trainer.py](../continuous_trainer.py) · [train_watchdog.py](../train_watchdog.py) · [monitor.py](../monitor.py) (자원 점유·진행률·실패 재기동).

```
[반복 학습 루프]
 학습 ──▶ 평가(eval_episodes) ──▶ 목표 달성?
   ▲                                 │ No
   └──── 보상 가중치 재조정 ◀──────────┘
                                     │ Yes → ONNX export → 배포
```

---

## 7. 평가 지표 및 결과

| 모델 | 평가 지표 | 목표/판정 기준 |
|---|---|---|
| 회피 SAC | 성공률, 충돌률 | 성공률 ≥ 70%, 충돌률 ≤ 15% |
| 출항 SAC | 액션 출력 sanity | confidence ∈ [−1, +1] 정상 출력 (29종 전수) |
| 연료 XGBoost | R², RMSE | R² 기반 배포 판정 (모델 메타 `metrics`) |
| YOLOv8 | mAP50, mAP50-95, precision, recall | 반복마다 메트릭 추적·개선 |
| What-If | RIO 분류 일관성 | 6~8개 시나리오·중복 0 보장 |

**검증 스크립트** · 모든 RL 모델은 더미 관측으로 로드·추론 정상 여부를 전수 점검한다.

- 회피 9종: [test_avoidance_models.py](../services-launcher/test_avoidance_models.py) (ROUTES × DIFFS, 환경 자동 관측)
- 출항 29종: [test_departure_models.py](../services-launcher/test_departure_models.py) (28-dim 더미 관측, OK/FAIL 집계)

---

## 8. 배포 및 운영

| 항목 | 내용 |
|---|---|
| 호스팅 | AWS EC2 (Docker 단일 컨테이너, t2.medium 2vCPU/4GB + swap 6GB) — Node 게이트웨이 8000 단일 진입점 |
| 프론트엔드 | Vercel (React + Cesium), `arctictwin.com` |
| 모델 저장 | Git LFS (~200MB) — ONNX/ZIP/PKL |
| 추론 런타임 | onnxruntime(CPU) — RL 배포 모델 |
| 동시 기동 | [services-launcher/](../services-launcher) — 5개 서비스 프로세스 분리 실행 |
| 자동 재학습 | [watchdog.py](../watchdog.py) · [train_watchdog.py](../train_watchdog.py) |
| 런타임 호환 | Python 3.14 + anyio/starlette 패치 ([rl-pipeline/server.py:15-34](../backend/services/rl-pipeline/server.py#L15-L34)) |

> **참고(현재 체크아웃 상태)** · 본 저장소는 포트폴리오용 부분 체크아웃으로, 일부 학습 소스(`rl_environment.py`, `rl_reward.py`, `rl_multi_model_trainer.py`)와 대용량 배포 모델 바이너리(출항 29종 ONNX, 연료 PKL)는 학습 머신에만 존재한다. 회피 ONNX 9종(`.onnx.data`)과 SB3 ZIP 백업은 포함되어 있다.

---

## 9. 부록 — 주요 파일 인덱스

| 구성요소 | 파일 | 핵심 내용 |
|---|---|---|
| 회피 RL 에이전트 | [rl_agent.py](../backend/services/rl-pipeline/modules/rl_agent.py) | SAC 하이퍼파라미터·모델 경로 |
| 회피 RL 서버 | [rl-pipeline/server.py](../backend/services/rl-pipeline/server.py) | 추론/학습 API, 멀티모델 조합 |
| 회피 정규화 스펙 | [NSR_normal_normalization.json](../model/avoidance-route/onnx/NSR_normal_normalization.json) | 22-dim 관측·2-dim 액션 범위 |
| 출항 RL 에이전트 | [departure_agent.py](../backend/services/report-service/modules/rl/departure_agent.py) | 29종 키 규칙·ONNX/ZIP 로드 |
| 연료 회귀 서버 | [ml-pipeline/server.py](../backend/services/ml-pipeline/server.py) | 6 feature·비용 상수·비교 API |
| 빙산 탐지 학습기 | [iceberg_model_trainer.py](../backend/pipeline/trainers/iceberg_model_trainer.py) | YOLOv8n 전이학습 설정 |
| 빙산 탐지 서버 | [sar_server.py](../sar_server.py) | 반복 학습·상태 API |
| What-If 생성기 | [whatif_generator_max.py](../backend/services/report-service/modules/whatif_generator_max.py) | Claude SDK·RIO 분류 |
| POLARIS 스코어러 | [route_scorer.py](../backend/services/report-service/modules/route_scorer.py) | 항로 세그먼트·농도→빙종 매핑 |
| 리포트 서버 | [report-service/server.py](../backend/services/report-service/server.py) | 리포트 생성·학습 오케스트레이션 |
| 회피 검증 | [test_avoidance_models.py](../services-launcher/test_avoidance_models.py) | 9종 로드/추론 점검 |
| 출항 검증 | [test_departure_models.py](../services-launcher/test_departure_models.py) | 29종 로드/추론 점검 |
| 프로젝트 개요 | [README.md](../README.md) | 아키텍처·기술 스택 |
