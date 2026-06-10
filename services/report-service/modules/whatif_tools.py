"""
whatif_tools.py
===============
Claude tool_use용 도구 정의.

RouteScorer, DataLoader를 래핑하여 Claude가 프로그래밍적으로
항로 평가를 수행할 수 있게 합니다.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict

from modules import db
from modules.sar_risk import assess_sar_risk

logger = logging.getLogger("report-service.whatif_tools")


# ── Claude Tool 스키마 정의 ──────────────────────────────────────
TOOL_DEFINITIONS = [
    {
        "name": "score_route",
        "description": (
            "특정 항로의 POLARIS RIO 위험도를 평가합니다. "
            "출항일 기준 forecast_days일간의 캘린더를 생성하고 "
            "green(안전)/yellow(주의)/red(위험) 일수를 집계합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "enum": ["NSR", "NWP", "TSR", "SUEZ", "CAPE"],
                    "description": "평가할 항로",
                },
                "ice_class": {
                    "type": "string",
                    "enum": ["PC1", "PC2", "PC3", "PC4", "PC5", "PC6", "PC7",
                             "IA Super", "IA", "IB", "IC", "None",
                             "Arc4", "Arc5", "Arc6", "Arc7", "Arc8", "Arc9"],
                    "description": (
                        "선박 Ice Class. IACS Polar Class(PC1~PC7), Baltic(IA Super~IC), "
                        "러시아 RMRS Arc(Arc4~Arc9) 표기 모두 지원 — Arc 는 가장 가까운 "
                        "IACS PC 등가로 자동 환산된다."
                    ),
                },
                "departure_date": {
                    "type": "string",
                    "description": "출항일 (YYYY-MM-DD)",
                },
                "forecast_days": {
                    "type": "integer",
                    "description": "예측 일수 (기본 30)",
                    "default": 30,
                },
            },
            "required": ["route", "ice_class", "departure_date"],
        },
    },
    {
        "name": "score_route_modified_ice",
        "description": (
            "해빙 농도를 조정한 가상 시나리오에서 항로를 평가합니다. "
            "ice_multiplier=1.3이면 해빙 농도가 30% 증가한 상황을 시뮬레이션합니다. "
            "기후변화, 이상 해빙 시나리오 분석에 사용합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "enum": ["NSR", "NWP", "TSR", "SUEZ", "CAPE"],
                },
                "ice_class": {
                    "type": "string",
                },
                "departure_date": {
                    "type": "string",
                },
                "forecast_days": {
                    "type": "integer",
                    "default": 30,
                },
                "ice_multiplier": {
                    "type": "number",
                    "description": "해빙 농도 배율 (0.5=절반, 1.0=현재, 1.5=50% 증가)",
                },
            },
            "required": ["route", "ice_class", "departure_date", "ice_multiplier"],
        },
    },
    {
        "name": "compare_ice_classes",
        "description": (
            "동일 항로를 여러 Ice Class로 비교 평가합니다. "
            "선박 업그레이드/다운그레이드 시나리오 분석에 사용합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "enum": ["NSR", "NWP", "TSR", "SUEZ", "CAPE"],
                },
                "departure_date": {
                    "type": "string",
                },
                "forecast_days": {
                    "type": "integer",
                    "default": 30,
                },
                "ice_classes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "비교할 Ice Class 목록 (예: ['PC5', 'PC3', 'IA Super'])",
                },
            },
            "required": ["route", "departure_date", "ice_classes"],
        },
    },
    {
        "name": "get_current_conditions",
        "description": (
            "현재 북극 해양 환경 데이터를 조회합니다. "
            "해빙 농도, 빙산 수, 기상 요약, SAR 탐지 현황을 반환합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── 도구 실행 함수 ──────────────────────────────────────────────
class WhatIfToolExecutor:
    """Claude tool_use 호출을 실제 로직으로 실행합니다."""

    def __init__(self, route_scorer, data_loader):
        self.scorer = route_scorer
        self.loader = data_loader
        self._monthly_ice_cache = None

    def _get_monthly_ice(self):
        """월별 해빙 데이터 캐싱 로드."""
        if self._monthly_ice_cache is None:
            self._monthly_ice_cache = self.loader.load_monthly_ice()
        return self._monthly_ice_cache

    def execute(self, tool_name: str, tool_input: dict) -> dict:
        """도구 이름과 입력으로 실행, 결과 반환."""
        handlers = {
            "score_route": self._score_route,
            "score_route_modified_ice": self._score_route_modified_ice,
            "compare_ice_classes": self._compare_ice_classes,
            "get_current_conditions": self._get_current_conditions,
        }

        handler = handlers.get(tool_name)
        if not handler:
            return {"error": f"알 수 없는 도구: {tool_name}"}

        try:
            return handler(tool_input)
        except Exception as e:
            logger.error("도구 실행 오류 (%s): %s", tool_name, e)
            return {"error": str(e)}

    def _score_route(self, inp: dict) -> dict:
        """항로 RIO 평가."""
        route = inp["route"]
        ice_class = inp["ice_class"]
        dep_date = date.fromisoformat(inp["departure_date"])
        days = inp.get("forecast_days", 30)

        monthly_ice = self._get_monthly_ice()

        scores = []
        for d in range(days):
            day = dep_date + timedelta(days=d)
            ds = self.scorer.score_departure_day(day, route, ice_class, monthly_ice)
            scores.append(ds)

        return self._summarize_scores(route, ice_class, scores)

    def _score_route_modified_ice(self, inp: dict) -> dict:
        """해빙 농도 조정 시나리오 평가."""
        route = inp["route"]
        ice_class = inp["ice_class"]
        dep_date = date.fromisoformat(inp["departure_date"])
        days = inp.get("forecast_days", 30)
        multiplier = inp["ice_multiplier"]

        monthly_ice = self._get_monthly_ice()

        # 농도 조정 복사본 생성
        modified_ice = {}
        for month, data in monthly_ice.items():
            modified_cells = []
            for cell in data.get("cells", []):
                new_cell = dict(cell)
                new_cell["concentration"] = min(1.0, cell.get("concentration", 0) * multiplier)
                modified_cells.append(new_cell)
            modified_ice[month] = {**data, "cells": modified_cells}

        scores = []
        for d in range(days):
            day = dep_date + timedelta(days=d)
            ds = self.scorer.score_departure_day(day, route, ice_class, modified_ice)
            scores.append(ds)

        result = self._summarize_scores(route, ice_class, scores)
        result["ice_multiplier"] = multiplier
        result["scenario"] = f"해빙 농도 ×{multiplier}"
        return result

    def _compare_ice_classes(self, inp: dict) -> dict:
        """여러 Ice Class 비교."""
        route = inp["route"]
        dep_date = date.fromisoformat(inp["departure_date"])
        days = inp.get("forecast_days", 30)
        ice_classes = inp["ice_classes"]

        monthly_ice = self._get_monthly_ice()
        comparison = {}

        for ic in ice_classes:
            scores = []
            for d in range(days):
                day = dep_date + timedelta(days=d)
                ds = self.scorer.score_departure_day(day, route, ic, monthly_ice)
                scores.append(ds)
            comparison[ic] = self._summarize_scores(route, ic, scores)

        return {"route": route, "comparison": comparison}

    def _get_current_conditions(self, _: dict) -> dict:
        """현재 해양 환경 요약."""
        latest = self.loader.load_latest_ice()
        bergs = self.loader.load_icebergs()
        weather = self.loader.load_weather()

        ice_stats = latest.get("stats", {})
        berg_stats = bergs.get("stats", {})

        # SAR 탐지 현황 확인 — DB(sar_detections) 최신 배치 우선, 실패 시 파일 폴백
        sar_info = self._get_sar_info()

        # SAR 원시 수치를 정량 위험 신호로 변환해 What-If(LLM·시나리오)에 연동
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sar_risk = assess_sar_risk(
            sar_info.get("sar_detected"),
            products_processed=sar_info.get("sar_products_processed"),
            detection_time=sar_info.get("sar_detection_time"),
            now_iso=now_iso,
        )

        return {
            "ice": {
                "arctic_cells": ice_stats.get("arctic_cells", 0),
                # load_latest_ice 가 내보내는 실제 키는 mean_conc / high_conc_pct 다.
                "mean_concentration": round(ice_stats.get("mean_conc", 0), 3),
                "high_concentration_pct": round(ice_stats.get("high_conc_pct", 0), 1),
            },
            "icebergs": {
                "total": berg_stats.get("total_count", 0),
                "arctic": berg_stats.get("arctic_count", 0),
                **sar_info,
                "sar_risk": sar_risk,
            },
            "weather": {
                route: {
                    "max_wave_m": summary.get("summary", {}).get("max_wave_m"),
                    "min_vis_km": summary.get("summary", {}).get("min_vis_km"),
                    "min_temp_c": summary.get("summary", {}).get("min_temp_c"),
                }
                for route, summary in weather.get("routes", {}).items()
                if isinstance(summary, dict)
            },
        }

    def _get_sar_info(self) -> dict:
        """SAR 탐지 현황. DB(sar_detections 최신 배치) 우선, 실패 시 파일 폴백."""
        # DB 우선
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
            except Exception as e:
                logger.warning("SAR DB 조회 실패 → 파일 폴백: %s", e)

        # 폴백: sar_detections_latest.json
        import json
        sar_file = self.loader.data_dir / "sar_detections_latest.json"
        if sar_file.exists():
            with open(sar_file, encoding="utf-8") as f:
                sar_data = json.load(f)
            return {
                "sar_detected": sar_data.get("total_detected", 0),
                "sar_detection_time": sar_data.get("detection_time", ""),
                "sar_products_processed": sar_data.get("products_processed", 0),
            }
        return {}

    @staticmethod
    def _summarize_scores(route: str, ice_class: str, scores: list) -> dict:
        """DayScore 목록을 요약 통계로 변환."""
        rios = [s.overall_rio for s in scores]
        colors = [s.color_code for s in scores]

        return {
            "route": route,
            "ice_class": ice_class,
            "total_days": len(scores),
            "avg_rio": round(sum(rios) / len(rios), 3) if rios else 0,
            "min_rio": round(min(rios), 3) if rios else 0,
            "max_rio": round(max(rios), 3) if rios else 0,
            "green_days": colors.count("green"),
            "yellow_days": colors.count("yellow"),
            "red_days": colors.count("red"),
            "safe_passage_pct": round(colors.count("green") / len(colors) * 100, 1) if colors else 0,
        }
