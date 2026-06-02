"""
whatif_generator.py
===================
Claude tool_use 기반 What-If 시나리오 자동 생성기.

현재 해양 데이터를 분석하여 의미 있는 시나리오를 자동 제안하고,
각 시나리오를 POLARIS RIO로 평가한 뒤 비교 분석 보고서를 생성합니다.

동작 흐름:
  1. 현재 환경 데이터 로드
  2. Claude API 호출 (tool_use 모드) — 핵심 3개 시나리오 (기준/추천/비추천)
  3. Claude가 시나리오 제안 + 도구 호출로 평가
  4. 하드코딩 풀에서 3~5개 무작위 보강 (총 6~8개)
  5. 비교 분석 + 추천 생성
  6. WhatIfResult 반환 (PDF 통합 또는 독립 API 응답)
"""

import json
import logging
import os
import random
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List, Optional, cast

from dotenv import load_dotenv

from .whatif_tools import TOOL_DEFINITIONS, WhatIfToolExecutor

logger = logging.getLogger("report-service.whatif_generator")

for _env in [
    Path(__file__).resolve().parents[3] / ".env",            # backend/services/report-service/modules → backend/.env
    Path(__file__).resolve().parents[3] / "backend" / ".env",
    Path(__file__).resolve().parents[2] / "backend" / ".env",
]:
    if _env.exists():
        load_dotenv(_env)
        break

WHATIF_SYSTEM_PROMPT = """당신은 북극 항로 전략 분석가입니다. IMO POLARIS 방법론, 선박 Ice Class 체계,
해빙 계절 변동에 정통하며, 데이터 기반으로 항로 개척 시나리오를 제안합니다.

당신의 역할:
1. 현재 해양 환경 데이터를 조회합니다 (get_current_conditions 도구 사용)
2. 데이터를 분석하여 의사결정자에게 가장 핵심적인 "정확히 3개"의 What-If 시나리오를 제안합니다
3. 각 시나리오를 도구를 사용하여 실제로 평가합니다
4. 결과를 비교하여 최종 추천을 제공합니다

3개 시나리오 구성 (반드시 준수):
- 시나리오 1: "기준 시나리오" — 현재 입력된 조건 그대로 평가
- 시나리오 2: "추천 시나리오" — 의사결정자에게 가장 유리한 단일 대안 (Ice Class 업그레이드,
  출항 시기 조정, 대안 항로 등 중 데이터상 가장 효과가 큰 것 1개)
- 시나리오 3: "비추천/위험 시나리오" — 의사결정자가 피해야 할 조건 (해빙 악화, 부적절한
  Ice Class, 시기 부적합 등 중 가장 명확한 위험 1개)

※ 시스템이 별도로 3~5개의 다양성 보강 시나리오(극한 조건, 모순 조합, 시점 변동 등)를
   하드코딩 풀에서 무작위로 추가합니다. 따라서 당신은 위 핵심 3개에만 집중하세요.

도구 활용 규칙:
- 반드시 먼저 get_current_conditions를 호출하여 현재 상황을 파악하세요
- 각 시나리오는 반드시 score_route 또는 score_route_modified_ice 도구로 평가하세요
- 최종 비교 분석은 반드시 한국어로 작성하세요
- 각 시나리오에 이름, 설명, 추천 이유를 포함하세요"""

SCENARIO_PROMPT_TEMPLATE = """현재 분석 조건:
- 기준 항로: {route}
- 선박 Ice Class: {ice_class}
- 출항 예정일: {departure_date}
- 예측 기간: {forecast_days}일

먼저 현재 환경 데이터를 조회한 뒤, 위 조건에서 "정확히 3개"의 핵심 What-If 시나리오를
제안하고, 각각을 도구로 평가한 뒤, 한국어로 비교 분석을 작성해주세요.

3개 시나리오 구성 (필수):
- 시나리오 1: 기준 시나리오 (현재 조건)
- 시나리오 2: 추천 시나리오 (가장 유리한 단일 대안)
- 시나리오 3: 비추천 시나리오 (피해야 할 위험 조건)

각 시나리오의 결과를 다음 형식으로 정리해주세요:
1. 시나리오 이름
2. 시나리오 설명 (왜 이 시나리오가 의미 있는지)
3. 평가 결과 요약 (avg_rio, green/yellow/red 일수)
4. 추천 여부 (추천/조건부/비추천)

마지막에 종합 추천을 작성해주세요.
※ 시스템이 별도로 3~5개의 다양성 보강 시나리오를 자동으로 추가하므로,
   당신은 위 3개에만 집중하면 됩니다."""


# ── 하드코딩 시나리오 풀 ─────────────────────────────────────────────
# Claude가 만드는 핵심 3개(기준/추천/비추천) 외에, 이 풀에서 매 호출마다
# 3~5개를 무작위 샘플링하여 추가합니다. 총 6~8개의 시나리오가 결과에 포함됩니다.
#
# 각 항목 스키마:
#   name        : 시나리오 표시 이름 (한국어)
#   description : 시나리오 설명 (왜 이 조합이 의미 있는지)
#   tool        : 호출할 도구 ("score_route" | "score_route_modified_ice")
#   overrides   : 기본 config에 덮어쓸 인자 dict.
#                 특수 키 departure_offset_days(int) 는 출항일 ±일수 보정으로 변환됩니다.
HARDCODED_SCENARIO_POOL: list[dict] = [
    # ════════════════════════════════════════════════════════════════
    # A. 해빙 농도 변동 (단일축) — 기후 변동 스펙트럼
    # ════════════════════════════════════════════════════════════════
    {"name": "재난급 결빙 (해빙 ×2.0)",
     "description": "해빙 농도가 두 배가 되는 재난급 결빙 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 2.0}},
    {"name": "강한 결빙 (해빙 +70%)",
     "description": "해빙 농도가 70% 증가한 강한 결빙 상황",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.7}},
    {"name": "극한 결빙 (해빙 +50%)",
     "description": "해빙 농도가 50% 증가한 극한 결빙 상황",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.5}},
    {"name": "명확한 악화 (해빙 +40%)",
     "description": "해빙 농도가 40% 증가하여 항행 윈도우가 좁아지는 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.4}},
    {"name": "조기 결빙 (해빙 +30%)",
     "description": "해빙 농도가 30% 증가하여 예년보다 일찍 결빙",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.3}},
    {"name": "약한 악화 (해빙 +20%)",
     "description": "해빙 농도가 20% 증가한 점진적 악화 상황",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.2}},
    {"name": "미세 악화 (해빙 +10%)",
     "description": "해빙이 10% 증가한 작은 변동 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.1}},
    {"name": "미세 개선 (해빙 -10%)",
     "description": "해빙이 10% 감소한 약간의 호조건",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.9}},
    {"name": "후기 해빙 (해빙 -20%)",
     "description": "해빙이 20% 감소하여 늦게 형성되는 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.8}},
    {"name": "보통 개선 (해빙 -30%)",
     "description": "해빙이 30% 감소하여 항행이 다소 수월해진 상태",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.7}},
    {"name": "명확한 개선 (해빙 -50%)",
     "description": "해빙이 절반으로 감소한 온난화 가속 상황",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.5}},
    {"name": "극한 온난화 (해빙 -70%)",
     "description": "해빙이 70% 감소하여 거의 무빙 상태에 가까운 극한 온난화",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.3}},

    # ════════════════════════════════════════════════════════════════
    # B. 출항 시기 변동 (단일축) — 계절 윈도우 탐색
    # ════════════════════════════════════════════════════════════════
    {"name": "한겨울 진입 (-90일)",
     "description": "3개월 일찍 출항하여 가장 두꺼운 결빙기에 진입",
     "tool": "score_route", "overrides": {"departure_offset_days": -90}},
    {"name": "결빙 정점 시기 (-60일)",
     "description": "2개월 조기 출항 (한겨울 직전 진입 위험)",
     "tool": "score_route", "overrides": {"departure_offset_days": -60}},
    {"name": "결빙 시작기 (-45일)",
     "description": "1.5개월 조기 출항으로 결빙기 진입을 평가",
     "tool": "score_route", "overrides": {"departure_offset_days": -45}},
    {"name": "1개월 조기 (-30일)",
     "description": "1개월 일찍 출항하여 항해 윈도우 변동 평가",
     "tool": "score_route", "overrides": {"departure_offset_days": -30}},
    {"name": "2주 조기 (-14일)",
     "description": "2주 조기 출항으로 단기 변동 흡수 가능성 평가",
     "tool": "score_route", "overrides": {"departure_offset_days": -14}},
    {"name": "2주 지연 (+14일)",
     "description": "2주 지연 출항으로 결빙 둔화 효과 검토",
     "tool": "score_route", "overrides": {"departure_offset_days": 14}},
    {"name": "1개월 지연 (+30일)",
     "description": "1개월 늦게 출항 시 해빙 감소 효과 평가",
     "tool": "score_route", "overrides": {"departure_offset_days": 30}},
    {"name": "1.5개월 지연 (+45일)",
     "description": "1.5개월 지연으로 한여름 진입에 가까워진 시나리오",
     "tool": "score_route", "overrides": {"departure_offset_days": 45}},
    {"name": "한여름 출항 (+60일)",
     "description": "2개월 지연하여 가장 따뜻한 시기에 출항",
     "tool": "score_route", "overrides": {"departure_offset_days": 60}},
    {"name": "늦여름 출항 (+75일)",
     "description": "2.5개월 지연 출항 (해빙 최저점 근방)",
     "tool": "score_route", "overrides": {"departure_offset_days": 75}},
    {"name": "초가을 (+90일)",
     "description": "3개월 지연 출항 (다시 결빙기에 진입할 위험)",
     "tool": "score_route", "overrides": {"departure_offset_days": 90}},
    {"name": "늦가을 (+120일)",
     "description": "4개월 지연 출항 (재결빙 본격화)",
     "tool": "score_route", "overrides": {"departure_offset_days": 120}},

    # ════════════════════════════════════════════════════════════════
    # C. 예측 기간 변동 — 단기/장기 항해 길이
    # ════════════════════════════════════════════════════════════════
    {"name": "초단기 항해 (7일)",
     "description": "1주일 단기 운항 시나리오 (긴급 수송)",
     "tool": "score_route", "overrides": {"forecast_days": 7}},
    {"name": "단기 항해 (14일)",
     "description": "2주일 단기 임시 운항 평가",
     "tool": "score_route", "overrides": {"forecast_days": 14}},
    {"name": "3주 항해 (21일)",
     "description": "3주 운항 윈도우 평가",
     "tool": "score_route", "overrides": {"forecast_days": 21}},
    {"name": "1.5개월 운항 (45일)",
     "description": "45일간의 중기 항해 시나리오",
     "tool": "score_route", "overrides": {"forecast_days": 45}},
    {"name": "2개월 운항 (60일)",
     "description": "60일간의 항해로 계절 변동 일부 포함",
     "tool": "score_route", "overrides": {"forecast_days": 60}},
    {"name": "3개월 장기 (90일)",
     "description": "90일 항해로 계절 전환을 포함한 평가",
     "tool": "score_route", "overrides": {"forecast_days": 90}},
    {"name": "4개월 장기 (120일)",
     "description": "120일 장기 운항 시나리오",
     "tool": "score_route", "overrides": {"forecast_days": 120}},
    {"name": "6개월 초장기 (180일)",
     "description": "반년 장기 미션 - 계절 두 번 전환 포함",
     "tool": "score_route", "overrides": {"forecast_days": 180}},

    # ════════════════════════════════════════════════════════════════
    # D. 대안 항로 (단일축) — 기본 항로 변경
    # ════════════════════════════════════════════════════════════════
    {"name": "북서항로 대체 (NWP)",
     "description": "NSR 대신 캐나다 북서항로 선택 시 안전성 평가",
     "tool": "score_route", "overrides": {"route": "NWP"}},
    {"name": "북극점 횡단 (TSR)",
     "description": "북극점을 직접 횡단하는 Transpolar Sea Route",
     "tool": "score_route", "overrides": {"route": "TSR"}},
    {"name": "수에즈 우회 (SUEZ)",
     "description": "전통 수에즈 운하 항로로 우회 (해빙 무관)",
     "tool": "score_route", "overrides": {"route": "SUEZ", "ice_class": "None"}},
    {"name": "희망봉 우회 (CAPE)",
     "description": "남아프리카 희망봉 우회 (장거리·해빙 무관)",
     "tool": "score_route", "overrides": {"route": "CAPE", "ice_class": "None"}},

    # ════════════════════════════════════════════════════════════════
    # E. Ice Class 변경 (단일축) — 선박 등급 스펙트럼
    # ════════════════════════════════════════════════════════════════
    {"name": "최고급 (PC1)",
     "description": "PC1 최고급 극지 선박 운용 (연중 모든 해빙 가능)",
     "tool": "score_route", "overrides": {"ice_class": "PC1"}},
    {"name": "고사양 (PC2)",
     "description": "PC2 고사양 선박 업그레이드 효과",
     "tool": "score_route", "overrides": {"ice_class": "PC2"}},
    {"name": "고사양 (PC3)",
     "description": "PC3 고사양 선박 운용 시 안전성",
     "tool": "score_route", "overrides": {"ice_class": "PC3"}},
    {"name": "중상급 (PC4)",
     "description": "PC4 선박 운용 시 적합성",
     "tool": "score_route", "overrides": {"ice_class": "PC4"}},
    {"name": "중하급 (PC6)",
     "description": "PC6 선박 운용 시 위험도 (제한된 운항 윈도우)",
     "tool": "score_route", "overrides": {"ice_class": "PC6"}},
    {"name": "최저 사양 (PC7)",
     "description": "PC7 저사양 선박 운용 시 위험도",
     "tool": "score_route", "overrides": {"ice_class": "PC7"}},
    {"name": "발틱 최상급 (IA Super)",
     "description": "발틱 IA Super 선박 운용 시 적합성",
     "tool": "score_route", "overrides": {"ice_class": "IA Super"}},
    {"name": "발틱 IA",
     "description": "발틱 IA 등급 선박 운용",
     "tool": "score_route", "overrides": {"ice_class": "IA"}},
    {"name": "발틱 IB",
     "description": "발틱 IB 등급 선박 운용 (제한적 결빙기 운항)",
     "tool": "score_route", "overrides": {"ice_class": "IB"}},
    {"name": "발틱 IC",
     "description": "발틱 IC 등급 (가장 약한 발틱 등급) 운용",
     "tool": "score_route", "overrides": {"ice_class": "IC"}},
    {"name": "비강화 선박 (None)",
     "description": "얼음 강화가 없는 일반 선박 운용 (극지 부적합)",
     "tool": "score_route", "overrides": {"ice_class": "None"}},

    # ════════════════════════════════════════════════════════════════
    # F. 항로 × Ice Class 조합
    # ════════════════════════════════════════════════════════════════
    {"name": "NSR + 최고급 (PC1)",
     "description": "북극항로에 PC1 투입 (잠재적 과잉 투자)",
     "tool": "score_route", "overrides": {"ice_class": "PC1"}},
    {"name": "NSR + PC4",
     "description": "북극항로 PC4 운용 (중간 사양)",
     "tool": "score_route", "overrides": {"ice_class": "PC4"}},
    {"name": "NSR + PC6 (저사양)",
     "description": "북극항로에 PC6 저사양 선박 운용 시 위험",
     "tool": "score_route", "overrides": {"ice_class": "PC6"}},
    {"name": "NSR + IA Super",
     "description": "발틱 IA Super 선박을 북극항로에 투입",
     "tool": "score_route", "overrides": {"ice_class": "IA Super"}},
    {"name": "NSR + 발틱 IA",
     "description": "발틱 IA 등급의 북극항로 적합성",
     "tool": "score_route", "overrides": {"ice_class": "IA"}},
    {"name": "NWP + PC2 고사양",
     "description": "캐나다 북서항로에 PC2 투입",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC2"}},
    {"name": "NWP + PC3",
     "description": "북서항로를 PC3 고사양 선박으로 운용",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC3"}},
    {"name": "NWP + PC4",
     "description": "북서항로 PC4 중간 사양 운용",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC4"}},
    {"name": "NWP + PC5",
     "description": "북서항로 PC5 표준 운용",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC5"}},
    {"name": "NWP + PC6 (저사양)",
     "description": "북서항로에 PC6 저사양 운용 (해협 통과 위험)",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC6"}},
    {"name": "NWP + PC7 (최저)",
     "description": "북서항로에 PC7 최저급 선박 운용 (자살적 시도)",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC7"}},
    {"name": "TSR + PC1 최고급",
     "description": "북극점 횡단 항로에 PC1 최고 사양",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "PC1"}},
    {"name": "TSR + PC2",
     "description": "북극점 횡단 항로에 PC2 운용",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "PC2"}},
    {"name": "TSR + PC3",
     "description": "북극점 횡단 항로 PC3 운용 (한계 등급)",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "PC3"}},
    {"name": "TSR + PC5 (위험)",
     "description": "북극점 횡단 항로에 PC5 표준 (불충분 가능성)",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "PC5"}},
    {"name": "TSR + IA Super (위험)",
     "description": "북극점 횡단에 발틱 IA Super (구조상 부적합)",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "IA Super"}},
    {"name": "SUEZ + PC5 (불필요 강화)",
     "description": "수에즈 항로에 극지 선박 투입 (불필요한 비용)",
     "tool": "score_route", "overrides": {"route": "SUEZ", "ice_class": "PC5"}},
    {"name": "CAPE + 비강화",
     "description": "희망봉 우회에 비강화 선박 (가장 경제적 선택)",
     "tool": "score_route", "overrides": {"route": "CAPE", "ice_class": "None"}},

    # ════════════════════════════════════════════════════════════════
    # G. Ice Class × 해빙 (모순/트레이드오프 - 예측 어려운 조합)
    # ════════════════════════════════════════════════════════════════
    {"name": "극단 과잉 (PC1 + 호조건)",
     "description": "PC1 최고급 선박이 해빙 -50% 호조건 운항 (자본 비효율)",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC1", "ice_multiplier": 0.5}},
    {"name": "심각 과잉 (PC2 + 호조건)",
     "description": "PC2 고사양이 해빙 -30% 호조건에서 운영비 낭비",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC2", "ice_multiplier": 0.7}},
    {"name": "보통 과잉 (PC3 + 호조건)",
     "description": "PC3 선박이 해빙 -20% 호조건에서 비용 효율 저하",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC3", "ice_multiplier": 0.8}},
    {"name": "균형점 (PC4 + 보통 악화)",
     "description": "PC4 선박이 해빙 +20% 약화 시 적정선 검증",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC4", "ice_multiplier": 1.2}},
    {"name": "한계 도전 (PC5 + 강한 악화)",
     "description": "PC5 표준 선박이 해빙 +50% 시 한계 평가",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC5", "ice_multiplier": 1.5}},
    {"name": "고위험 (PC5 + 극한 결빙)",
     "description": "PC5가 해빙 +70% 극한 결빙을 만났을 때",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC5", "ice_multiplier": 1.7}},
    {"name": "이상 조합 (PC6 + 호조건)",
     "description": "PC6 저사양이 해빙 -30% 호조건에서 가까스로 통과",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC6", "ice_multiplier": 0.7}},
    {"name": "심각 위험 (PC6 + 보통 악화)",
     "description": "PC6 저사양이 해빙 +30% 악화 시 운항 한계",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC6", "ice_multiplier": 1.3}},
    {"name": "치명 위험 (PC6 + 극한)",
     "description": "PC6이 해빙 +50% 극한 상황 - 운항 불가능",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC6", "ice_multiplier": 1.5}},
    {"name": "이상 조합 (PC7 + 호조건)",
     "description": "PC7 최저급이 해빙 -50% 극호조건에서만 운항 가능",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC7", "ice_multiplier": 0.5}},
    {"name": "언더스펙 (PC7 + 보통 악화)",
     "description": "PC7 최저급이 해빙 +30% 악화 시 운항 위험",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC7", "ice_multiplier": 1.3}},
    {"name": "치명 (PC7 + 강한 악화)",
     "description": "PC7이 해빙 +50% 강한 악화 - 운항 절대 불가",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC7", "ice_multiplier": 1.5}},
    {"name": "발틱 IA + 보통 악화",
     "description": "발틱 IA가 해빙 +20% 시 발틱 본연 한계 평가",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "IA", "ice_multiplier": 1.2}},
    {"name": "발틱 IA Super + 강한 악화",
     "description": "IA Super가 해빙 +50% 극한 시 발틱 등급 최대치",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "IA Super", "ice_multiplier": 1.5}},

    # ════════════════════════════════════════════════════════════════
    # H. 항로 × 해빙 변동 조합
    # ════════════════════════════════════════════════════════════════
    {"name": "NSR + 강한 악화",
     "description": "북극항로 + 해빙 +50% 시 항행 실현 가능성",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.5}},
    {"name": "NSR + 극한 악화",
     "description": "북극항로 + 해빙 +70% 극한 결빙 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.7}},
    {"name": "NSR + 강한 호조건",
     "description": "북극항로 + 해빙 -50% 시 항행 윈도우 확장",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.5}},
    {"name": "NWP + 보통 악화",
     "description": "북서항로 + 해빙 +30% 시 해협 통과 위험",
     "tool": "score_route_modified_ice", "overrides": {"route": "NWP", "ice_multiplier": 1.3}},
    {"name": "NWP + 강한 악화",
     "description": "북서항로 + 해빙 +50% (해협 봉쇄 위험)",
     "tool": "score_route_modified_ice", "overrides": {"route": "NWP", "ice_multiplier": 1.5}},
    {"name": "NWP + 호조건",
     "description": "북서항로 + 해빙 -30% 시 해협 통과 가능성",
     "tool": "score_route_modified_ice", "overrides": {"route": "NWP", "ice_multiplier": 0.7}},
    {"name": "TSR + 약한 악화",
     "description": "북극점 횡단 + 해빙 +20% 시 횡단 가능성",
     "tool": "score_route_modified_ice", "overrides": {"route": "TSR", "ice_multiplier": 1.2}},
    {"name": "TSR + 강한 악화",
     "description": "북극점 횡단 + 해빙 +50% (횡단 거의 불가)",
     "tool": "score_route_modified_ice", "overrides": {"route": "TSR", "ice_multiplier": 1.5}},
    {"name": "TSR + 극한 호조건",
     "description": "북극점 횡단 + 해빙 -50% (이론적 통과 가능)",
     "tool": "score_route_modified_ice", "overrides": {"route": "TSR", "ice_multiplier": 0.5}},

    # ════════════════════════════════════════════════════════════════
    # I. 항로 × 출항 시기 조합
    # ════════════════════════════════════════════════════════════════
    {"name": "NSR + 한겨울 진입",
     "description": "북극항로를 60일 조기 출항하여 한겨울 진입",
     "tool": "score_route", "overrides": {"departure_offset_days": -60}},
    {"name": "NSR + 한여름 출항",
     "description": "북극항로를 60일 지연하여 한여름 항행",
     "tool": "score_route", "overrides": {"departure_offset_days": 60}},
    {"name": "NWP + 조기 출항",
     "description": "북서항로 30일 조기 출항 시 해협 결빙 위험",
     "tool": "score_route", "overrides": {"route": "NWP", "departure_offset_days": -30}},
    {"name": "NWP + 한여름",
     "description": "북서항로 60일 지연 (해빙 최저점에 정렬)",
     "tool": "score_route", "overrides": {"route": "NWP", "departure_offset_days": 60}},
    {"name": "NWP + 늦가을",
     "description": "북서항로 90일 지연 (재결빙 시작)",
     "tool": "score_route", "overrides": {"route": "NWP", "departure_offset_days": 90}},
    {"name": "TSR + 한여름",
     "description": "북극점 횡단을 한여름(+60일)에 시도",
     "tool": "score_route", "overrides": {"route": "TSR", "departure_offset_days": 60}},
    {"name": "TSR + 조기 출항",
     "description": "북극점 횡단을 30일 조기 시도 (가장 위험)",
     "tool": "score_route", "overrides": {"route": "TSR", "departure_offset_days": -30}},
    {"name": "TSR + 늦여름",
     "description": "북극점 횡단을 75일 지연 (해빙 최저)",
     "tool": "score_route", "overrides": {"route": "TSR", "departure_offset_days": 75}},

    # ════════════════════════════════════════════════════════════════
    # J. 복합 3축 시나리오 (예측 어려운 다축 변동)
    # ════════════════════════════════════════════════════════════════
    {"name": "이상 한파 + 조기 출항",
     "description": "해빙 +30% 상태에서 1개월 조기 출항 (복합 위험)",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.3, "departure_offset_days": -30}},
    {"name": "온난기 + 늦은 출항",
     "description": "해빙 -30% 상태에서 1개월 지연 출항 (이중 호조건)",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.7, "departure_offset_days": 30}},
    {"name": "이상 한파 + 한겨울 진입",
     "description": "해빙 +50% 상태에서 60일 조기 출항 (자살적)",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 1.5, "departure_offset_days": -60}},
    {"name": "강한 온난화 + 한여름",
     "description": "해빙 -50% 상태에서 60일 지연 한여름 출항 (최선의 조건)",
     "tool": "score_route_modified_ice", "overrides": {"ice_multiplier": 0.5, "departure_offset_days": 60}},
    {"name": "NSR + PC3 + 강한 악화",
     "description": "PC3 고사양으로 북극항로 + 해빙 +50% 도전",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC3", "ice_multiplier": 1.5}},
    {"name": "NSR + PC7 + 보통 악화",
     "description": "PC7 최저급으로 해빙 +30% 시 재앙 시나리오",
     "tool": "score_route_modified_ice", "overrides": {"ice_class": "PC7", "ice_multiplier": 1.3}},
    {"name": "NWP + PC3 + 보통 악화",
     "description": "북서항로 + PC3 + 해빙 +30% (해협 통과 한계)",
     "tool": "score_route_modified_ice", "overrides": {"route": "NWP", "ice_class": "PC3", "ice_multiplier": 1.3}},
    {"name": "NWP + PC5 + 1개월 지연",
     "description": "북서항로 PC5 + 30일 지연 (한여름 정렬)",
     "tool": "score_route", "overrides": {"route": "NWP", "ice_class": "PC5", "departure_offset_days": 30}},
    {"name": "TSR + PC2 + 약한 악화",
     "description": "북극점 횡단 PC2 + 해빙 +20% (한계 검증)",
     "tool": "score_route_modified_ice", "overrides": {"route": "TSR", "ice_class": "PC2", "ice_multiplier": 1.2}},
    {"name": "TSR + PC3 + 1개월 지연",
     "description": "북극점 횡단 PC3 + 30일 지연 (한여름 정렬)",
     "tool": "score_route", "overrides": {"route": "TSR", "ice_class": "PC3", "departure_offset_days": 30}},
    {"name": "장기 + 강한 악화 (90일)",
     "description": "90일 장기 + 해빙 +50% 강한 악화 누적 효과",
     "tool": "score_route_modified_ice", "overrides": {"forecast_days": 90, "ice_multiplier": 1.5}},
    {"name": "장기 + 호조건 (90일)",
     "description": "90일 장기 + 해빙 -30% 호조건 (시즌 통과)",
     "tool": "score_route_modified_ice", "overrides": {"forecast_days": 90, "ice_multiplier": 0.7}},
    {"name": "NSR + PC5 + 한겨울",
     "description": "PC5 표준선이 60일 조기 출항하여 한겨울 진입",
     "tool": "score_route", "overrides": {"ice_class": "PC5", "departure_offset_days": -60}},
    {"name": "NSR + PC3 + 한여름",
     "description": "PC3 고사양이 60일 지연 한여름 출항 (가장 안전)",
     "tool": "score_route", "overrides": {"ice_class": "PC3", "departure_offset_days": 60}},
    {"name": "SUEZ + 단기",
     "description": "수에즈 항로 14일 단기 운항 시나리오",
     "tool": "score_route", "overrides": {"route": "SUEZ", "ice_class": "None", "forecast_days": 14}},
    {"name": "CAPE + 장기 (180일)",
     "description": "희망봉 우회 6개월 장기 미션",
     "tool": "score_route", "overrides": {"route": "CAPE", "ice_class": "None", "forecast_days": 180}},

    # ════════════════════════════════════════════════════════════════
    # K. 비즈니스/전략 시나리오 (현실적 의사결정)
    # ════════════════════════════════════════════════════════════════
    {"name": "수에즈 봉쇄 시 NSR 긴급",
     "description": "수에즈 운하 봉쇄 가정 시 NSR 단기 긴급 운영",
     "tool": "score_route", "overrides": {"ice_class": "PC4", "forecast_days": 14}},
    {"name": "보험 요구 업그레이드",
     "description": "보험사 요구로 PC3 고사양 업그레이드",
     "tool": "score_route", "overrides": {"ice_class": "PC3"}},
    {"name": "운영비 절감 다운그레이드",
     "description": "선박 운영비 절감을 위해 PC6 다운그레이드",
     "tool": "score_route", "overrides": {"ice_class": "PC6"}},
    {"name": "공급망 위기 다변화",
     "description": "글로벌 공급망 위기 대응으로 CAPE 우회",
     "tool": "score_route", "overrides": {"route": "CAPE", "ice_class": "None"}},
    {"name": "겨울철 강제 운항",
     "description": "PC2 고사양으로 60일 조기 강제 운항",
     "tool": "score_route", "overrides": {"ice_class": "PC2", "departure_offset_days": -60}},
    {"name": "여름 전용 운항",
     "description": "PC6 저사양으로 60일 지연 여름 한정 운항",
     "tool": "score_route", "overrides": {"ice_class": "PC6", "departure_offset_days": 60}},
    {"name": "장기 미션 (90일 + PC4)",
     "description": "PC4 선박으로 90일 장기 미션 검증",
     "tool": "score_route", "overrides": {"ice_class": "PC4", "forecast_days": 90}},
    {"name": "단기 임시 항해 (14일)",
     "description": "PC5 선박으로 14일 단기 임시 항해",
     "tool": "score_route", "overrides": {"forecast_days": 14}},
    {"name": "신규 항로 시범 (NWP + 60일)",
     "description": "북서항로 60일 시범 운영 (신규 항로 평가)",
     "tool": "score_route", "overrides": {"route": "NWP", "forecast_days": 60}},
    {"name": "발틱 선박 도입 (IA Super)",
     "description": "발틱 IA Super 선박 도입 시 북극항로 적합성",
     "tool": "score_route", "overrides": {"ice_class": "IA Super"}},
    {"name": "비강화 선박 강행",
     "description": "비강화 선박으로 북극항로 무리한 강행",
     "tool": "score_route", "overrides": {"ice_class": "None"}},
    {"name": "최고급 정기 운항 (PC1)",
     "description": "PC1 최고급 선박을 정기 운항으로 활용",
     "tool": "score_route", "overrides": {"ice_class": "PC1", "forecast_days": 60}},
]

HARDCODED_MIN = 3
HARDCODED_MAX = 5
CLAUDE_SCENARIO_CAP = 3


@dataclass
class ScenarioResult:
    name: str
    description: str
    route_summary: dict
    recommendation: str  # 추천/조건부/비추천


@dataclass
class WhatIfResult:
    scenarios: list[ScenarioResult] = field(default_factory=list)
    comparison_text: str = ""
    ai_recommendation: str = ""
    tool_calls_count: int = 0


class WhatIfGenerator:
    """Claude tool_use 기반 What-If 시나리오 생성기."""

    MAX_TOOL_ITERATIONS = 15

    def __init__(self, route_scorer, data_loader):
        self.tool_executor = WhatIfToolExecutor(route_scorer, data_loader)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(
                    api_key=os.environ.get("ANTHROPIC_API_KEY", "")
                )
            except ImportError:
                logger.error("anthropic 패키지가 필요합니다: pip install anthropic")
                raise
        return self._client

    def generate_scenarios(
        self,
        route: str = "NSR",
        ice_class: str = "PC5",
        departure_date: str = "",
        forecast_days: int = 30,
    ) -> WhatIfResult:
        """
        What-If 시나리오를 자동 생성하고 평가합니다.

        Claude가 tool_use를 통해 자율적으로 시나리오를 제안하고 평가합니다.
        """
        if not departure_date:
            departure_date = date.today().isoformat()

        prompt = SCENARIO_PROMPT_TEMPLATE.format(
            route=route,
            ice_class=ice_class,
            departure_date=departure_date,
            forecast_days=forecast_days,
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_calls_count = 0
        # 도구 호출 결과 수집: score_route / score_route_modified_ice / compare_ice_classes 결과만 저장
        collected_route_summaries: list[dict] = []

        # Claude tool_use 루프
        for iteration in range(self.MAX_TOOL_ITERATIONS):
            try:
                response = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4000,
                    system=WHATIF_SYSTEM_PROMPT,
                    tools=cast(Any, TOOL_DEFINITIONS),
                    messages=cast(Any, messages),
                )
            except Exception as e:
                logger.error("Claude API 호출 실패: %s", e)
                return self._fallback_result(route, ice_class, departure_date, forecast_days)

            # 응답 처리
            if response.stop_reason == "tool_use":
                # Claude가 도구를 호출함
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        tool_calls_count += 1
                        tool_name = block.name
                        tool_input = block.input

                        logger.info("도구 호출 [%d]: %s(%s)",
                                    tool_calls_count, tool_name, json.dumps(tool_input, ensure_ascii=False)[:100])

                        exec_result = self.tool_executor.execute(tool_name, tool_input)

                        # 항로 평가 결과 수집 (시나리오 route_summary 구성에 활용)
                        if tool_name in ("score_route", "score_route_modified_ice"):
                            if "avg_rio" in exec_result:
                                collected_route_summaries.append(exec_result)
                        elif tool_name == "compare_ice_classes":
                            # compare 결과에서 각 ice_class별 summary 추출
                            for ic_summary in exec_result.get("comparison", {}).values():
                                if "avg_rio" in ic_summary:
                                    collected_route_summaries.append(ic_summary)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(exec_result, ensure_ascii=False),
                        })

                # 어시스턴트 메시지 + 도구 결과를 대화에 추가
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            elif response.stop_reason == "end_turn":
                # Claude가 분석을 완료함
                final_text = self._extract_text(response.content)
                result = self._parse_result(final_text, collected_route_summaries)
                result.tool_calls_count = tool_calls_count
                self._augment_with_hardcoded(
                    result, route, ice_class, departure_date, forecast_days,
                )
                logger.info("What-If 분석 완료: %d 시나리오, %d 도구 호출",
                            len(result.scenarios), tool_calls_count)
                return result
            else:
                logger.warning("예상치 못한 stop_reason: %s", response.stop_reason)
                break

        # 최대 반복 초과
        logger.warning("최대 반복(%d) 초과", self.MAX_TOOL_ITERATIONS)
        final_text = self._extract_text(messages[-1].get("content", []) if isinstance(messages[-1], dict) else [])
        result = self._parse_result(final_text, collected_route_summaries)
        result.tool_calls_count = tool_calls_count
        self._augment_with_hardcoded(
            result, route, ice_class, departure_date, forecast_days,
        )
        return result

    def _extract_text(self, content) -> str:
        """Claude 응답에서 텍스트만 추출합니다."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if hasattr(block, "text"):
                    texts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return "\n".join(texts)
        return str(content)

    def _parse_result(self, text: str, route_summaries: list[dict] | None = None) -> WhatIfResult:
        """Claude의 텍스트 응답을 구조화된 WhatIfResult로 파싱합니다.

        route_summaries: 도구 호출로 수집된 항로 평가 결과 목록.
          시나리오 순서대로 매핑합니다.
        """
        result = WhatIfResult()
        result.comparison_text = text
        if route_summaries is None:
            route_summaries = []

        # 시나리오 파싱 (텍스트 휴리스틱)
        lines = text.split("\n")
        current_scenario = None
        scenario_idx = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # "시나리오 1:", "1.", "### 시나리오" 등의 패턴 감지
            if any(kw in stripped.lower() for kw in ["시나리오", "scenario"]) and any(c.isdigit() for c in stripped):
                if current_scenario:
                    result.scenarios.append(current_scenario)
                    scenario_idx += 1
                # 수집된 route_summary를 순서대로 매핑
                rs = route_summaries[scenario_idx] if scenario_idx < len(route_summaries) else {}
                current_scenario = ScenarioResult(
                    name=stripped.lstrip("#").strip(),
                    description="",
                    route_summary=rs,
                    recommendation="",
                )
            elif current_scenario:
                if "추천" in stripped or "recommend" in stripped.lower():
                    if "비추천" in stripped or "불가" in stripped:
                        current_scenario.recommendation = "비추천"
                    elif "조건부" in stripped:
                        current_scenario.recommendation = "조건부"
                    else:
                        current_scenario.recommendation = "추천"
                elif not current_scenario.description and len(stripped) > 10:
                    current_scenario.description = stripped

            # 종합 추천 감지
            if "종합" in stripped and ("추천" in stripped or "결론" in stripped):
                idx = lines.index(line)
                result.ai_recommendation = "\n".join(
                    l.strip() for l in lines[idx:] if l.strip()
                )

        if current_scenario:
            result.scenarios.append(current_scenario)

        # 시나리오가 파싱되지 않았으면 수집된 route_summaries로 기본 시나리오 구성
        if not result.scenarios and route_summaries:
            for i, rs in enumerate(route_summaries):
                result.scenarios.append(ScenarioResult(
                    name=f"시나리오 {i + 1}",
                    description=rs.get("scenario", f"{rs.get('route', '')} / {rs.get('ice_class', '')}"),
                    route_summary=rs,
                    recommendation="추천" if rs.get("green_days", 0) > rs.get("red_days", 0) else "조건부",
                ))
            result.ai_recommendation = text
        elif not result.scenarios:
            result.ai_recommendation = text

        return result

    def _fallback_result(
        self,
        route: str,
        ice_class: str,
        departure_date: str,
        forecast_days: int,
    ) -> WhatIfResult:
        """Claude API 실패 시 기본 시나리오를 직접 생성합니다.

        하드코딩 풀에서 추가 보강이 호출되므로, 최종적으로 6~8개 시나리오를 반환합니다.
        """
        logger.info("Fallback: 기본 시나리오 3개 자동 생성")

        scenarios = []

        # 시나리오 1: 현재 조건 (기준)
        base = self.tool_executor.execute("score_route", {
            "route": route,
            "ice_class": ice_class,
            "departure_date": departure_date,
            "forecast_days": forecast_days,
        })
        scenarios.append(ScenarioResult(
            name="기준 시나리오 (현재 조건)",
            description=f"{route} 항로, {ice_class} 선박, 현재 해빙 조건",
            route_summary=base,
            recommendation="기준",
        ))

        # 시나리오 2: 해빙 +30%
        worse = self.tool_executor.execute("score_route_modified_ice", {
            "route": route,
            "ice_class": ice_class,
            "departure_date": departure_date,
            "forecast_days": forecast_days,
            "ice_multiplier": 1.3,
        })
        scenarios.append(ScenarioResult(
            name="해빙 악화 시나리오 (+30%)",
            description="해빙 농도가 30% 증가한 상황 (이상 기후, 조기 결빙)",
            route_summary=worse,
            recommendation="비추천" if worse.get("red_days", 0) > 10 else "조건부",
        ))

        # 시나리오 3: Ice Class 업그레이드
        upgraded_class = "PC3" if ice_class in ("PC5", "PC4") else "PC2"
        upgrade = self.tool_executor.execute("score_route", {
            "route": route,
            "ice_class": upgraded_class,
            "departure_date": departure_date,
            "forecast_days": forecast_days,
        })
        scenarios.append(ScenarioResult(
            name=f"선박 업그레이드 ({ice_class}→{upgraded_class})",
            description=f"Ice Class를 {upgraded_class}로 상향 시 개선 효과",
            route_summary=upgrade,
            recommendation="추천" if upgrade.get("green_days", 0) > 20 else "조건부",
        ))

        result = WhatIfResult(
            scenarios=scenarios,
            comparison_text="Claude API 연결 실패로 기본 시나리오 3개를 자동 생성했습니다.",
            ai_recommendation="기본 시나리오 비교 결과를 참고하시기 바랍니다.",
            tool_calls_count=3,
        )
        self._augment_with_hardcoded(
            result, route, ice_class, departure_date, forecast_days,
        )
        return result

    # ── 하드코딩 시나리오 보강 ─────────────────────────────────
    def _augment_with_hardcoded(
        self,
        result: WhatIfResult,
        base_route: str,
        base_ice_class: str,
        base_departure_date: str,
        base_forecast_days: int,
    ) -> None:
        """결과를 in-place로 보강한다.

        - Claude/기준 시나리오를 정확히 CLAUDE_SCENARIO_CAP개로 제한
        - 하드코딩 풀에서 HARDCODED_MIN~HARDCODED_MAX개를 무작위 추출하여 추가
        - 기존 시나리오와 동일한 (route, ice_class, avg_rio, total_days) 조합은 중복으로 보고 제외
        - 최종 결과는 6~8개의 시나리오를 포함하는 것이 목표
        """
        result.scenarios = result.scenarios[:CLAUDE_SCENARIO_CAP]

        seen_keys = {self._scenario_key(s) for s in result.scenarios}

        target = random.randint(HARDCODED_MIN, HARDCODED_MAX)
        # 중복 발생 가능성을 감안해 target보다 넉넉히 추출 후 dedupe
        extras = self._generate_hardcoded_scenarios(
            base_route, base_ice_class, base_departure_date, base_forecast_days,
            target * 2,
        )

        added = 0
        for ex in extras:
            if added >= target:
                break
            key = self._scenario_key(ex)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            result.scenarios.append(ex)
            added += 1

        logger.info("하드코딩 시나리오 %d개 보강 — 중복 제외 후 총 %d개",
                    added, len(result.scenarios))

    @staticmethod
    def _scenario_key(scenario) -> tuple:
        """시나리오 동등성 판정 키 — 항로/선박/기간/avg_rio가 같으면 같은 시나리오로 간주."""
        rs = getattr(scenario, "route_summary", None) or {}
        return (
            rs.get("route", ""),
            rs.get("ice_class", ""),
            rs.get("total_days", 0),
            round(rs.get("avg_rio", 0.0), 3),
            round(rs.get("ice_multiplier", 1.0), 2),
        )

    def _generate_hardcoded_scenarios(
        self,
        base_route: str,
        base_ice_class: str,
        base_departure_date: str,
        base_forecast_days: int,
        count: int,
    ) -> list[ScenarioResult]:
        """HARDCODED_SCENARIO_POOL에서 count개를 무작위로 추출하여 평가합니다."""
        pool = list(HARDCODED_SCENARIO_POOL)
        sampled = random.sample(pool, min(count, len(pool)))

        try:
            base_dep_obj = date.fromisoformat(base_departure_date)
        except ValueError:
            base_dep_obj = date.today()

        scenarios: list[ScenarioResult] = []

        for template in sampled:
            params = {
                "route": base_route,
                "ice_class": base_ice_class,
                "departure_date": base_departure_date,
                "forecast_days": base_forecast_days,
            }
            overrides = dict(template["overrides"])
            offset_days = overrides.pop("departure_offset_days", 0)
            if offset_days:
                params["departure_date"] = (
                    base_dep_obj + timedelta(days=offset_days)
                ).isoformat()
            params.update(overrides)

            try:
                route_summary = self.tool_executor.execute(template["tool"], params)
            except Exception as e:
                logger.warning("하드코딩 시나리오 실행 오류 [%s]: %s",
                               template["name"], e)
                continue

            if not isinstance(route_summary, dict) or "error" in route_summary:
                logger.warning("하드코딩 시나리오 결과 오류 [%s]: %s",
                               template["name"], route_summary)
                continue

            scenarios.append(ScenarioResult(
                name=template["name"],
                description=template["description"],
                route_summary=route_summary,
                recommendation=self._classify_recommendation(route_summary),
            ))

        return scenarios

    @staticmethod
    def _classify_recommendation(route_summary: dict) -> str:
        """POLARIS RIO 점수와 안전 통과율 기반 분류.

        IMO POLARIS 방법론에서 RIO(Risk Index Outcome)는 정량 지표로:
          - rio < 0      : Elevated Operations (주의 운항 필요)
          - rio ≥ 0      : 일반 운항 가능

        본 분류는 의사결정 보조용으로 RIO를 세 단계로 세분화:
          - avg_rio ≥ 1.5  : 충분한 안전 여유 → 추천
          - 0.0 ≤ avg_rio < 1.5 : 운항 가능하나 여유 부족 → 조건부
          - avg_rio < 0.0  : Elevated Operations 영역 → 비추천

        보조 강등 규칙: avg_rio가 추천 임계 이상이라도 safe_passage_pct가
        50% 미만이면 yellow/red 일수가 많다는 뜻이므로 한 단계 강등.
        """
        avg_rio = route_summary.get("avg_rio")
        if avg_rio is None:
            # avg_rio 없으면 보수적으로 조건부
            return "조건부"

        safe_pct = route_summary.get("safe_passage_pct", 100.0)

        if avg_rio < 0.0:
            return "비추천"
        if avg_rio >= 1.5:
            return "조건부" if safe_pct < 50.0 else "추천"
        return "조건부"
