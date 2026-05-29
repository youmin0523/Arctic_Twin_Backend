"""
data_loader.py
==============
backend/data/ 에 저장된 JSON 데이터를 읽어 통계를 계산하는 모듈.

사용 데이터:
  - realIceData_month01~12.json  (월별 해빙 농도, 롤링 12개월)
  - realIceData_latest.json      (최신 해빙 농도 - 대용량)
  - realBergData_latest.json     (빙산 현황)
  - weather_latest.json          (Open-Meteo 5개 항로 기상)
  - arctic_weather_latest.json   (MET Norway NSR 기상, 보조)
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("report-service.data_loader")

# 두 가지 레이아웃 지원:
#   - 로컬: Digital_twin/backend/services/report-service/modules/data_loader.py
#           → parents[3] = Digital_twin/
#   - HF Space: <root>/report-service/modules/data_loader.py
#           → parents[2] = <root>
_CANDIDATES = [
    Path(__file__).resolve().parents[3] / "data",            # backend/services/report-service/modules → backend/data
    Path(__file__).resolve().parents[3] / "backend" / "data",
    Path(__file__).resolve().parents[2] / "backend" / "data",
]
DATA_DIR = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])


class DataLoader:
    """JSON 데이터 로드 및 통계 계산."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or DATA_DIR

    # ── 월별 해빙 ──────────────────────────────────────────────
    def load_monthly_ice(self, months: list[int] | None = None) -> dict[int, dict]:
        """월별 해빙 데이터 로드. {month_num: {source, date, cells, stats}}"""
        if months is None:
            months = list(range(1, 13))

        result = {}
        for m in months:
            fpath = self.data_dir / "monthly" / f"realIceData_month{m:02d}.json"
            if not fpath.exists():
                logger.warning("월별 해빙 파일 없음: %s", fpath.name)
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                cells = data.get("cells", [])
                concs = [c["concentration"] for c in cells if 0 < c["concentration"] <= 1]
                data["stats"] = {
                    "mean_conc": round(float(np.mean(concs)), 4) if concs else 0,
                    "max_conc": round(float(np.max(concs)), 4) if concs else 0,
                    "min_conc": round(float(np.min(concs)), 4) if concs else 0,
                    "cell_count": len(concs),
                    "high_conc_pct": round(
                        sum(1 for c in concs if c >= 0.8) / len(concs) * 100, 1
                    ) if concs else 0,
                    "arctic_coverage_pct": round(
                        sum(1 for c in concs if c >= 0.15) / len(concs) * 100, 1
                    ) if concs else 0,
                }
                result[m] = data
            except Exception as e:
                logger.error("월별 해빙 로드 실패 (month %d): %s", m, e)
        return result

    # ── 최신 해빙 (대용량 → lat > 65 필터) ─────────────────────
    def load_latest_ice(self) -> dict:
        """최신 해빙 데이터 로드 (위도 65도 이상만)."""
        fpath = self.data_dir / "realIceData_latest.json"
        if not fpath.exists():
            logger.warning("최신 해빙 파일 없음: %s", fpath)
            return {"cells": [], "stats": {}}

        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)

        cells = data.get("cells", [])
        arctic_cells = [c for c in cells if c.get("lat", 0) > 65]
        concs = [c["concentration"] for c in arctic_cells if 0 < c["concentration"] <= 1]

        data["arctic_cells"] = arctic_cells
        data["stats"] = {
            "total_cells": len(cells),
            "arctic_cells": len(arctic_cells),
            "mean_conc": round(float(np.mean(concs)), 4) if concs else 0,
            "high_conc_pct": round(
                sum(1 for c in concs if c >= 0.8) / len(concs) * 100, 1
            ) if concs else 0,
        }
        # 원본 cells는 메모리 절약을 위해 제거
        del data["cells"]
        return data

    # ── 빙산 ───────────────────────────────────────────────────
    def load_icebergs(self) -> dict:
        """빙산 현황 로드."""
        fpath = self.data_dir / "realBergData_latest.json"
        if not fpath.exists():
            logger.warning("빙산 파일 없음: %s", fpath)
            return {"bergs": [], "stats": {}}

        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)

        bergs = data.get("bergs", [])
        # 북극권 빙산만 필터 (위도 > 60)
        arctic_bergs = [b for b in bergs if b.get("lat", 0) > 60]
        all_bergs = bergs

        data["stats"] = {
            "total_count": len(all_bergs),
            "arctic_count": len(arctic_bergs),
            "avg_length_m": round(
                float(np.mean([b.get("length_m", 0) for b in all_bergs])), 1
            ) if all_bergs else 0,
            "types": {},
        }
        for b in all_bergs:
            t = b.get("type", "unknown")
            data["stats"]["types"][t] = data["stats"]["types"].get(t, 0) + 1

        return data

    # ── 기상 ───────────────────────────────────────────────────
    def load_weather(self) -> dict:
        """기상 데이터 로드 (Open-Meteo 5개 항로 + MET Norway 보조)."""
        result = {"routes": {}, "arctic": {}}

        # 메인: weather_latest.json (Open-Meteo, 5개 항로)
        main_path = self.data_dir / "weather_latest.json"
        if main_path.exists():
            with open(main_path, encoding="utf-8") as f:
                main_data = json.load(f)
            result["fetched_at"] = main_data.get("fetched_at")
            result["source"] = main_data.get("source")
            routes = main_data.get("routes", {})
            for route_key, route_data in routes.items():
                wps = route_data.get("waypoints", [])
                waves = [w["wave_height_m"] for w in wps if w.get("wave_height_m") is not None]
                temps = [w["temperature_c"] for w in wps if w.get("temperature_c") is not None]
                vis = [w["visibility_km"] for w in wps if w.get("visibility_km") is not None]
                ssts = [w["sst_c"] for w in wps if w.get("sst_c") is not None]
                result["routes"][route_key] = {
                    "waypoints": wps,
                    "summary": {
                        "max_wave_m": round(max(waves), 2) if waves else None,
                        "avg_wave_m": round(float(np.mean(waves)), 2) if waves else None,
                        "min_temp_c": round(min(temps), 1) if temps else None,
                        "avg_temp_c": round(float(np.mean(temps)), 1) if temps else None,
                        "min_vis_km": round(min(vis), 1) if vis else None,
                        "avg_sst_c": round(float(np.mean(ssts)), 1) if ssts else None,
                    },
                }

        # 보조: arctic_weather_latest.json (MET Norway, NSR)
        arctic_path = self.data_dir / "arctic_weather_latest.json"
        if arctic_path.exists():
            with open(arctic_path, encoding="utf-8") as f:
                arctic_data = json.load(f)
            result["arctic"] = {
                "source": arctic_data.get("source"),
                "waypoints": arctic_data.get("waypoints", []),
            }

        return result

    # ── AI 프롬프트용 월별 요약 ────────────────────────────────
    def build_monthly_summary(self) -> list[dict[str, Any]]:
        """12개월 통계 요약 (AI 분석 입력용)."""
        monthly = self.load_monthly_ice()
        summary = []
        for m in range(1, 13):
            if m not in monthly:
                summary.append({"month": m, "available": False})
                continue
            data = monthly[m]
            stats = data.get("stats", {})
            summary.append({
                "month": m,
                "available": True,
                "date": data.get("date", ""),
                "source": data.get("source", ""),
                "mean_concentration": stats.get("mean_conc", 0),
                "max_concentration": stats.get("max_conc", 0),
                "cell_count": stats.get("cell_count", 0),
                "high_concentration_pct": stats.get("high_conc_pct", 0),
                "arctic_coverage_pct": stats.get("arctic_coverage_pct", 0),
            })
        return summary
