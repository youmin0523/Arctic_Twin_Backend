"""sar_risk.py

SAR(YOLOv8) 빙산 탐지 결과를 What-If 의사결정에 쓸 수 있는 **정량 위험 신호**로 변환.

기존엔 SAR 탐지 수(raw count)만 LLM 도구에 노출돼, 시나리오 평가에 구조적으로
연동되지 않았다(발표자료 통합 갭 #2의 SAR↔What-If). 이 순수 함수는 탐지 밀도·
신선도(최신성)·규모를 종합해 위험 등급과 권고를 산출한다(테스트 가능, 의존성 없음).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """'YYYY-MM-DDTHH:MM:SSZ' 등 ISO 문자열을 datetime 으로(실패 시 None)."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def freshness_hours(detection_time: Optional[str], now_iso: Optional[str]) -> Optional[float]:
    """탐지 시각으로부터 경과 시간(시간). 입력 부족 시 None."""
    det = _parse_iso(detection_time)
    now = _parse_iso(now_iso)
    if det is None or now is None:
        return None
    delta = (now - det).total_seconds() / 3600.0
    return round(max(0.0, delta), 2)


def assess_sar_risk(
    sar_detected: Optional[int],
    products_processed: Optional[int] = None,
    detection_time: Optional[str] = None,
    now_iso: Optional[str] = None,
    *,
    stale_hours: float = 48.0,
) -> dict:
    """SAR 탐지 현황을 구조화된 위험 평가로 변환.

    Returns dict:
      level      : 'none' | 'low' | 'moderate' | 'high' | 'unknown'
      detected   : 정규화된 탐지 수(int)
      density     : 산출물당 평균 탐지 수 (products 있을 때)
      stale       : 탐지가 오래됐는지(bool|None)
      age_hours   : 경과 시간(float|None)
      note        : 사람이 읽을 요약
    """
    # 데이터 없음
    if sar_detected is None:
        return {
            "level": "unknown", "detected": 0, "density": None,
            "stale": None, "age_hours": None,
            "note": "SAR 탐지 데이터 없음 — 위성 영상 미수신 또는 미처리",
        }

    detected = max(0, int(sar_detected))
    age = freshness_hours(detection_time, now_iso)
    stale = None if age is None else age > stale_hours

    density = None
    if products_processed and products_processed > 0:
        density = round(detected / products_processed, 2)

    # 위험 등급: 탐지 수 기반 + 밀도 가중
    if detected == 0:
        level = "none"
    elif detected >= 30 or (density is not None and density >= 10):
        level = "high"
    elif detected >= 10 or (density is not None and density >= 3):
        level = "moderate"
    else:
        level = "low"

    # 오래된 데이터는 한 단계 하향(신뢰도 저하) — high→moderate, moderate→low
    if stale and level in ("high", "moderate"):
        level = "moderate" if level == "high" else "low"

    notes = {
        "none": "SAR 빙산 미탐지 — 위성 영상상 위협 없음",
        "low": f"SAR 빙산 {detected}건 탐지 — 경계 항행",
        "moderate": f"SAR 빙산 {detected}건 탐지 — 회피 경로·감속 권고",
        "high": f"SAR 빙산 {detected}건 다수 탐지 — 쇄빙선 에스코트·출항 재검토 권고",
    }
    note = notes[level]
    if stale and age is not None:
        note += f" (탐지 {age:.0f}h 경과 — 최신 영상 갱신 권장)"

    return {
        "level": level,
        "detected": detected,
        "density": density,
        "stale": stale,
        "age_hours": age,
        "note": note,
    }
