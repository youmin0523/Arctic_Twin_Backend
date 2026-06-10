"""
chat_tools.py
=============
북극항로 전용 챗봇(chat_agent)이 "모델 총동원"에 쓰는 **신규 도구** 정의·실행기.

설계 원칙:
- 기존 What-If 도구(`whatif_tools.TOOL_DEFINITIONS` / `WhatIfToolExecutor`)는 그대로 재사용한다.
  여기서는 그 위에 얹는 추가 도구만 정의한다(연료/경제성, 기상, 쇄빙선, 빙산 위험).
- 도구 스키마는 기존과 동일한 Anthropic 형식(`input_schema`)으로 적되, `to_openai_tools()`로
  OpenAI function-calling 형식으로 변환해 쓴다.
- `recommend_departure` / `launch_full_report` / `launch_full_whatif` 는 server.py 의 싱글톤
  (route_scorer, departure_agent, _create_job, _generate_report …)에 의존하므로 여기서 정의하지
  않고 server.py 가 핸들러를 주입한다.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# pipeline 패키지(쇄빙선 함대/항로 로더)를 import 할 수 있도록 backend/ 를 sys.path 에 추가.
# (report-service 런타임 경로엔 backend/ 가 없어 'pipeline' 미해결 → 함대/거리 도구 실패 방지)
_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from modules.sar_risk import assess_sar_risk
from modules import db

logger = logging.getLogger("report-service.chat_tools")

# 내부 ML 연료 서비스 (Node 게이트웨이와 동일 호스트에서 구동) — existing_rl_client 패턴과 동일
FUEL_BASE_URL = "http://127.0.0.1:8003"


# ── 부산→로테르담 항로별 총 거리(해리) 근사 폴백 ────────────────────────────────
# 정식 거리는 frontend/src/data/arcticRoutes.js 웨이포인트 합산이 진실원이나, 백엔드 단독
# 배포(프론트 폴더 부재) 시를 대비해 널리 인용되는 부산-로테르담 항로별 거리를 폴백으로 둔다.
# 챗봇 추정 도구이므로 가정을 명시해 사용한다.
# 값은 arcticRoutes.js 웨이포인트 합산(부산↔로테르담) 실측치 기준. 프론트 부재 시 폴백.
ROUTE_DISTANCE_NM: dict[str, float] = {
    "NSR": 7974.0,    # 북동항로 (시베리아 연안)
    "NWP": 8490.0,    # 북서항로 (캐나다 군도)
    "TSR": 7268.0,    # 횡단극항로 (북극점 인근, 최단)
    "SUEZ": 11024.0,  # 수에즈 운하 경유
    "CAPE": 14965.0,  # 희망봉 우회
}

# 선종별 기본 제원 (사용자가 일부만 줘도 동작하도록) — ML /api/fuel/compare 입력 기준
DEFAULT_SHIP_SPECS: dict[str, dict] = {
    "container": {"displacement": 120_000, "draft": 14.0, "engine_power": 60_000, "speed_knots": 16.0},
    "lng":       {"displacement": 100_000, "draft": 12.5, "engine_power": 45_000, "speed_knots": 17.0},
    "icebreaker":{"displacement": 25_000,  "draft": 11.0, "engine_power": 45_000, "speed_knots": 14.0},
}

# Ice Class → 연료모델 ice_class_code (0=없음, 2=PC2급 강내빙, 4=PC4급 내빙)
_ARC_TO_PC = {"Arc9": "PC3", "Arc7": "PC4", "Arc6": "PC5", "Arc5": "PC6", "Arc4": "PC7"}


def ice_class_to_code(ice_class: str | None) -> int:
    """'PC5'/'Arc4'/'IA' 등 → 연료모델 ice_class_code(0/2/4) 근사 매핑."""
    if not ice_class:
        return 0
    ic = _ARC_TO_PC.get(ice_class, ice_class).upper().replace(" ", "")
    if ic in ("PC1", "PC2", "PC3"):
        return 2          # 강내빙(쇄빙 가능급)
    if ic in ("PC4", "PC5", "PC6", "PC7", "IASUPER", "IA"):
        return 4          # 내빙급
    return 0              # IB/IC/None 등 → 내빙 없음


def route_total_nm(route: str, loader=None) -> float:
    """항로 총 거리(해리). 가능하면 arcticRoutes.js 웨이포인트 합산, 실패 시 상수 폴백."""
    route = (route or "NSR").upper()
    try:
        from pipeline.icebreaker.routes_loader import load_routes
        from pipeline.icebreaker.icebreaker_dispatcher import _km_between
        routes = load_routes()
        pts = routes.get(route)
        if pts and len(pts) >= 2:
            km = sum(_km_between(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            if km > 0:
                return round(km / 1.852, 1)
    except Exception as e:  # noqa: BLE001  프론트 부재/파싱 실패 → 폴백
        logger.debug("route_total_nm 실측 실패(%s) → 폴백 사용", e)
    return ROUTE_DISTANCE_NM.get(route, ROUTE_DISTANCE_NM["NSR"])


def resolve_ship_spec(partial: dict | None) -> dict:
    """선종 기본값 위에 사용자가 준 값을 덮어써 완전한 제원 dict 반환."""
    partial = partial or {}
    vtype = (partial.get("vessel_type") or "container").lower()
    if vtype not in DEFAULT_SHIP_SPECS:
        vtype = "container"
    spec = dict(DEFAULT_SHIP_SPECS[vtype])
    spec["vessel_type"] = vtype
    for k in ("displacement", "draft", "engine_power", "speed_knots"):
        if partial.get(k) is not None:
            spec[k] = partial[k]
    return spec


# ── 신규 도구 스키마 (Anthropic 형식 — whatif_tools.TOOL_DEFINITIONS 와 동일 컨벤션) ──
CHAT_TOOL_DEFINITIONS = [
    {
        "name": "compare_economics",
        "description": (
            "북극항로 vs 수에즈 운하의 총 부대비용·경제성을 XGBoost 연료예측 모델로 비교한다. "
            "연료비·쇄빙선 호위료·북극보험·수에즈 통행료/보안비를 모두 합산해 총비용과 절감액을 산출한다. "
            "'총 부대비용', '타당성/경제성 검토', 'NSR vs 수에즈' 질문에 사용한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": ["NSR", "NWP", "TSR"],
                          "description": "북극항로 (수에즈와 비교됨)"},
                "departure_date": {"type": "string",
                                   "description": "출항일 YYYY-MM-DD (해당 '월'의 해빙 농도로 평가 — 계절 반영). 생략 시 현재."},
                "vessel_type": {"type": "string", "enum": ["container", "lng", "icebreaker"],
                                "description": "선종 (기본 container)"},
                "ice_class": {"type": "string",
                              "description": "선박 Ice Class (PC1~PC7 / IA~IC / Arc4~9). 연료모델 내빙코드로 환산됨."},
                "displacement": {"type": "number", "description": "배수량(tons), 선택"},
                "draft": {"type": "number", "description": "흘수(m), 선택"},
                "engine_power": {"type": "number", "description": "엔진출력(kW), 선택"},
                "speed_knots": {"type": "number", "description": "운항속도(knots), 선택"},
            },
            "required": ["route"],
        },
    },
    {
        "name": "get_route_weather",
        "description": (
            "특정 항로의 실시간 기상 요약(최대 파고, 최저 기온, 최저 가시거리, 평균 해수온)을 반환한다. "
            "Open-Meteo 기반. 파고·해수온·기상 질문에 사용한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": ["NSR", "NWP", "TSR", "SUEZ", "CAPE"]},
            },
            "required": ["route"],
        },
    },
    {
        "name": "get_escort_status",
        "description": (
            "특정 항로의 쇄빙선 호위 함대 현황(보유 쇄빙선·내빙등급·쇄빙 가능 두께·사전배치 모항·상태)과, "
            "선박 RIO가 주어지면 호위가 필요한 위험 수준인지 판정해 반환한다. 쇄빙선 관련 질문에 사용한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": ["NSR", "NWP", "TSR", "ROSS", "PENINSULA"]},
                "ice_class": {"type": "string", "description": "선박 Ice Class (호위 필요 판정용), 선택"},
                "rio": {"type": "number", "description": "현재/예상 RIO 값 (호위 필요 판정용), 선택"},
            },
            "required": ["route"],
        },
    },
    {
        "name": "get_iceberg_risk",
        "description": (
            "Sentinel-1 SAR 실측 빙산 탐지 현황을 정량 위험등급(none/low/moderate/high)·탐지수·신선도로 반환한다. "
            "빙산 위험·안전 항행 질문에 사용한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "description": "관심 항로 (참고용), 선택"},
            },
            "required": [],
        },
    },
]


def to_openai_tools(defs: list) -> list:
    """Anthropic 형식(input_schema) → OpenAI function-calling 형식 변환."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in defs
    ]


class ChatToolExecutor:
    """챗봇 신규 도구(데이터/외부서비스 의존)의 실행기.

    server.py 의 싱글톤에 의존하지 않는 도구만 담당한다:
    compare_economics, get_route_weather, get_escort_status, get_iceberg_risk.
    (recommend_departure / launch_full_* 는 server.py 가 별도 핸들러로 주입.)
    """

    NAMES = {"compare_economics", "get_route_weather", "get_escort_status", "get_iceberg_risk"}

    def __init__(self, data_loader):
        self.loader = data_loader

    async def execute(self, name: str, args: dict) -> dict:
        try:
            if name == "compare_economics":
                return await self._compare_economics(args)
            if name == "get_route_weather":
                return self._get_route_weather(args)
            if name == "get_escort_status":
                return self._get_escort_status(args)
            if name == "get_iceberg_risk":
                return self._get_iceberg_risk(args)
            return {"error": f"알 수 없는 도구: {name}"}
        except Exception as e:  # noqa: BLE001  도구 실패가 대화를 끊지 않도록 방어
            logger.error("chat 도구 실행 오류 (%s): %s", name, e)
            return {"error": str(e)}

    # ── compare_economics ─────────────────────────────────────────
    async def _compare_economics(self, args: dict) -> dict:
        route = (args.get("route") or "NSR").upper()
        spec = resolve_ship_spec(args)
        ice_code = ice_class_to_code(args.get("ice_class"))

        # NSR 빙질 입력. 출항일이 주어지면 그 '월'의 해빙 농도(계절 반영), 없으면 현재 스냅샷.
        # mean_conc 는 '빙존재 셀 평균'이라 항행 구간보다 과대할 수 있어 대표 농도로 상한(0.6).
        dep = args.get("departure_date")
        mean_conc = 0.0
        if dep:
            try:
                from datetime import date as _date
                month = _date.fromisoformat(dep).month
                monthly = self.loader.load_monthly_ice([month])
                cells = monthly.get(month, {}).get("cells", [])
                concs = [c.get("concentration", 0) for c in cells if (c.get("concentration") or 0) > 0]
                if concs:
                    mean_conc = sum(concs) / len(concs)
            except Exception:  # noqa: BLE001  월 데이터 실패 → 현재 스냅샷 폴백
                mean_conc = 0.0
        if mean_conc <= 0:
            latest = self.loader.load_latest_ice()
            mean_conc = float(latest.get("stats", {}).get("mean_conc", 0.0) or 0.0)
        route_conc = min(mean_conc, 0.6)
        assumptions = {
            "ice_concentration_used": round(route_conc, 3),
            "ice_concentration_arctic_mean": round(mean_conc, 3),
            "ice_thickness_m": 1.2,
            "note": "빙농도는 항행구간 대표값(상한 0.6) 적용 — 전역 평균은 팩아이스 포함이라 과대. "
                    "빙두께는 실측 스칼라 부재로 여름철 1년빙 1.2m 가정.",
        }

        payload = {
            "displacement": spec["displacement"],
            "draft": spec["draft"],
            "engine_power": spec["engine_power"],
            "ice_class_code": ice_code,
            "nsr_ice_thickness": assumptions["ice_thickness_m"],
            "nsr_ice_concentration": route_conc,
            "nsr_distance_nm": route_total_nm(route, self.loader),
            "suez_distance_nm": route_total_nm("SUEZ", self.loader),
            "vessel_type": spec["vessel_type"],
            "speed_knots": spec["speed_knots"],
            "route": route,  # 호위비 자국(NSR=아라온)/타국(NWP·TSR) 분기용
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{FUEL_BASE_URL}/api/fuel/compare", json=payload)
                r.raise_for_status()
                result = r.json()
        except Exception as e:  # noqa: BLE001
            return {"error": f"연료/경제성 서비스 호출 실패: {e}",
                    "hint": "ML 서비스(:8003) 미가동일 수 있음", "request": payload}

        result["_input"] = {"route": route, **{k: payload[k] for k in (
            "vessel_type", "ice_class_code", "nsr_distance_nm", "suez_distance_nm", "speed_knots")}}
        result["_assumptions"] = assumptions
        return result

    # ── get_route_weather ─────────────────────────────────────────
    def _get_route_weather(self, args: dict) -> dict:
        route = (args.get("route") or "NSR").upper()
        weather = self.loader.load_weather()
        rdata = weather.get("routes", {}).get(route)
        if not rdata:
            return {"route": route, "error": "해당 항로 기상 데이터 없음"}
        return {
            "route": route,
            "fetched_at": weather.get("fetched_at"),
            "summary": rdata.get("summary", {}),
        }

    # ── get_escort_status ─────────────────────────────────────────
    def _get_escort_status(self, args: dict) -> dict:
        route = (args.get("route") or "NSR").upper()
        from pipeline.icebreaker.models import fleet_for_route
        fleet = fleet_for_route(route)
        out = {
            "route": route,
            "fleet": [
                {
                    "name": ib.get("name_ko"),
                    "ice_class": ib.get("ice_class"),
                    "breakable_thickness_m": ib.get("breakable_thickness_m"),
                    "home_port": ib.get("home_port"),
                    "speed_knots": ib.get("speed_knots"),
                    "status": ib.get("status"),
                }
                for ib in fleet
            ],
        }
        ice_class = args.get("ice_class")
        rio = args.get("rio")
        if ice_class is not None and rio is not None:
            try:
                from pipeline.icebreaker.icebreaker_dispatcher import (
                    needs_icebreaker, threshold_for_ice_class,
                )
                out["escort_needed"] = bool(needs_icebreaker(float(rio), ice_class))
                out["call_threshold_rio"] = round(float(threshold_for_ice_class(ice_class)), 3)
            except Exception as e:  # noqa: BLE001
                out["escort_needed_error"] = str(e)
        return out

    # ── get_iceberg_risk ──────────────────────────────────────────
    def _get_iceberg_risk(self, args: dict) -> dict:
        info = self._sar_info()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        risk = assess_sar_risk(
            info.get("sar_detected"),
            products_processed=info.get("sar_products_processed"),
            detection_time=info.get("sar_detection_time"),
            now_iso=now_iso,
        )
        bergs = self.loader.load_icebergs()
        return {
            "route": args.get("route"),
            "sar_risk": risk,
            "icebergs_total": bergs.get("stats", {}).get("total_count", 0),
            "icebergs_arctic": bergs.get("stats", {}).get("arctic_count", 0),
        }

    def _sar_info(self) -> dict:
        """SAR 탐지 최신 배치. DB 우선, 파일 폴백 (whatif_tools 와 동일 규칙)."""
        if db.db_available():
            try:
                rows = db.fetch_all(
                    """
                    SELECT count(*)::int AS total_detected,
                           to_char(max(detection_time),
                                   'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS detection_time,
                           max(products_processed) AS products_processed
                      FROM sar_detections
                     WHERE detection_time = (SELECT max(detection_time) FROM sar_detections)
                    """
                )
                if rows and rows[0]["total_detected"]:
                    return {
                        "sar_detected": rows[0]["total_detected"],
                        "sar_detection_time": rows[0]["detection_time"] or "",
                        "sar_products_processed": rows[0]["products_processed"] or 0,
                    }
            except Exception as e:  # noqa: BLE001
                logger.warning("SAR DB 조회 실패 → 파일 폴백: %s", e)
        import json
        sar_file = self.loader.data_dir / "sar_detections_latest.json"
        if sar_file.exists():
            with open(sar_file, encoding="utf-8") as f:
                data = json.load(f)
            return {
                "sar_detected": data.get("total_detected", 0),
                "sar_detection_time": data.get("detection_time", ""),
                "sar_products_processed": data.get("products_processed", 0),
            }
        return {}
