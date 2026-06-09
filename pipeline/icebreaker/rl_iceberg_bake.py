"""
rl_iceberg_bake.py
==================
Voyage 시뮬레이션 트레이스에 "RL 빙산 회피"를 오프라인으로 사전 베이크한다.

배경
----
`simulate_voyage` 의 본선은 고정 route 를 그대로 따라가며 빙산 회피를 하지 않는다.
프론트엔드 Voyage 재생 모드는 이 trace 의 본선 좌표(ticks[].ship.position)를 그대로
재생하므로, 핵심 기능인 RL 회피가 선미추적(FOLLOW) 화면에 전혀 보이지 않는다.

이 모듈은 trace 생성 단계에서 route 를 따라 빙산성(berg-like) 위협을 감지하고, 학습된
RL 모델(RLTrainer.infer)로 우회 경로를 받아 route 에 splice 한다. 결과적으로 본선 좌표
자체가 위협을 휘어 피하게 되고, 회피 구간은 metadata.rl_avoidance.segments +
rl_avoid_start/end 이벤트로 표시되어 재생만으로 RL 회피가 결정적으로 재현된다.

위협 소스
---------
북극 항로에는 실측 빙산(realBergData)이 거의 없다(대부분 남극). 대신 `IceField` 의
**고두께 셀(>=3.0m, Ridged/Hummocked·Glacier Ice)** 을 berg-like 위협으로 사용한다.
이는 ice_type_mapper 가 극지 한겨울 두꺼운 빙역의 embedded iceberg/glacier fragment
worst-case 를 3.5m Glacier Ice 로 표현하는 것과 일치하며, 광범위한 pack ice(에스코트
대상)가 아니라 sparse 한 최악 지점만 골라 "피해갈 대상"으로 삼는다.

안전 설계
---------
- RL 의존성(torch/stable-baselines3, 학습 가중치)은 **지연 import**. 미설치/모델 없음 시
  apply_rl_iceberg_avoidance 는 원본 경로를 그대로 반환(applied=False) → CI 안전.
- 프론트 rlAvoidanceController.js 의 가드를 최소 포팅:
  is_projected_path_sane / is_detour_forward / is_waypoint_list_sane.
- splice 후 land_mask.refine_route 로 육지 재정합. 최종 경로가 비정상이면 전체 폐기(원본 유지).
- 결정성: 진입 시 random/np.random seed 고정.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Any, cast

import numpy as np

from pipeline.icebreaker.icebreaker_dispatcher import bearing, offset_position
from pipeline.icebreaker.models import Position

# ─── 튜닝 상수 ──────────────────────────────────────────────────────────────
# 위협 판정 두께. 항로변 최대 두께가 NSR~2.7 / NWP~3.1 / TSR~3.5m 이므로 2.0m
# (Multi-Year/두꺼운 압력능선)로 잡아야 세 항로 모두에서 sparse 한 worst-spot 가
# 잡힌다. 광범위 pack ice 회피가 아니라 국소 최악점만: MAX_DETOURS 로 상한.
THREAT_MIN_THICK_M = 2.0
THREAT_BUFFER_KM = 25.0       # route 중심선에서 이 거리 이내 위협만 대상(코리도 반폭)
CLUSTER_GAP_KM = 80.0         # 이 간격 이상 떨어진 위협은 별도 회피 클러스터
MAX_DETOURS = 6               # trace 당 최대 회피 횟수(두께 상위 클러스터 우선) — JSON/추론량 상한
MAX_BERGS_PER_INFER = 12      # 클러스터당 RL infer 입력 빙산 수 상한(학습 분포 3~15개 정합)
APPROACH_MARGIN_KM = 40.0     # 위협 중심 이 거리 전부터 디투어 시작
LOOKAHEAD_KM = 70.0           # RL 투영 구간 길이(rl_trainer.SEG_KM 와 정합)
MIN_CONFIDENCE = 0.3          # 이 미만이면 디투어 폐기(원본 유지)
DEG_TO_KM = 111.32

# route 당 RLTrainer 1회 로드 캐시(108 trace 재생성 시 모델 재로드 방지)
_TRAINER_CACHE: dict[str, Any] = {}
_RL_IMPORT_FAILED = False


def _log(msg: str) -> None:
    """cp949 콘솔에서 비-한글 기호(em-dash 등)로 죽지 않는 안전 출력."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── 거리/지오메트리 (날짜변경선 안전 근사) ────────────────────────────────
def _dist(a: Position, b: Position) -> float:
    """두 좌표 간 근사 거리 km. 경도차를 [-180,180]로 정규화."""
    dlat = (b["lat"] - a["lat"]) * DEG_TO_KM
    dlon_deg = b["lon"] - a["lon"]
    if dlon_deg > 180:
        dlon_deg -= 360
    elif dlon_deg < -180:
        dlon_deg += 360
    dlon = dlon_deg * DEG_TO_KM * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return math.hypot(dlat, dlon)


def _point_seg_km(pt: Position, a: Position, b: Position) -> float:
    """점 pt 에서 선분 a→b 까지 최단 거리 km (cos-lat 평면 근사)."""
    cl = math.cos(math.radians(pt["lat"]))
    ax = (b["lon"] - a["lon"]) * cl
    ay = b["lat"] - a["lat"]
    px = (pt["lon"] - a["lon"]) * cl
    py = pt["lat"] - a["lat"]
    L2 = ax * ax + ay * ay
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, (px * ax + py * ay) / L2))
    cx = ax * t - px
    cy = ay * t - py
    return math.hypot(cx, cy) * DEG_TO_KM


def _cross_track_km(pt: Position, route: list[Position]) -> float:
    best = math.inf
    for i in range(len(route) - 1):
        d = _point_seg_km(pt, route[i], route[i + 1])
        if d < best:
            best = d
    return best


def _segment_distances_km(route: list[Position]) -> list[float]:
    return [_dist(route[i], route[i + 1]) for i in range(len(route) - 1)]


def _cumulative_km(route: list[Position]) -> list[float]:
    cum = [0.0]
    for s in _segment_distances_km(route):
        cum.append(cum[-1] + s)
    return cum


def _position_at_km(route: list[Position], cum: list[float], km: float) -> Position:
    """누적거리 km 위치를 great-circle 보간(simulate_voyage._position_at_km 와 동일 규칙)."""
    if km <= 0.0:
        return cast("Position", dict(route[0]))
    total = cum[-1]
    if km >= total:
        return cast("Position", dict(route[-1]))
    for i in range(len(cum) - 1):
        if cum[i] <= km <= cum[i + 1]:
            seg_len = cum[i + 1] - cum[i]
            if seg_len <= 0:
                return cast("Position", dict(route[i]))
            frac = (km - cum[i]) / seg_len
            brng = bearing(route[i], route[i + 1])
            return offset_position(route[i], brng, frac * seg_len)
    return cast("Position", dict(route[-1]))


def _nearest_route_km(pt: Position, route: list[Position], cum: list[float]) -> float:
    best_i, best_d = 0, math.inf
    for i, p in enumerate(route):
        d = _dist(pt, p)
        if d < best_d:
            best_d, best_i = d, i
    return cum[best_i]


def _idx_at_or_before(cum: list[float], km: float) -> int:
    idx = 0
    for i, c in enumerate(cum):
        if c <= km:
            idx = i
        else:
            break
    return idx


def _idx_at_or_after(cum: list[float], km: float) -> int:
    for i, c in enumerate(cum):
        if c >= km:
            return i
    return len(cum) - 1


# ─── 안전 가드 (rlAvoidanceController.js 포팅) ──────────────────────────────
def _max_segment_km(route: list[Position]) -> float:
    if not route or len(route) < 2:
        return 0.0
    return max(_dist(route[i - 1], route[i]) for i in range(1, len(route)))


def is_projected_path_sane(path: list[Position], ship: Position, max_dist_km: float = 400.0) -> bool:
    """RL 투영 경로 폭주(극점/날짜변경선 특이점) 가드. (JS isProjectedPathSane)"""
    if not path:
        return False
    for p in path:
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon):
            return False
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            return False
        if _dist(ship, p) > max_dist_km:
            return False
    return True


def is_detour_forward(path: list[Position], start: Position, end: Position,
                      backtrack_tol: float = 0.15) -> bool:
    """디투어가 start→end 축으로 단조 전진하는지(자기교차 방지). (JS isDetourForward)"""
    if len(path) < 2:
        return False
    cos_lat = math.cos(math.radians(start["lat"]))
    ax = (end["lon"] - start["lon"]) * cos_lat
    ay = end["lat"] - start["lat"]
    axis_len2 = ax * ax + ay * ay
    if axis_len2 < 1e-9:
        return True
    max_proj = -math.inf
    for p in path:
        if not math.isfinite(p["lon"]) or not math.isfinite(p["lat"]):
            return False
        proj = ((p["lon"] - start["lon"]) * cos_lat * ax + (p["lat"] - start["lat"]) * ay) / axis_len2
        if proj < max_proj - backtrack_tol:
            return False
        if proj > max_proj:
            max_proj = proj
    return True


def is_waypoint_list_sane(route: list[Position], original: list[Position],
                          abs_floor_km: float = 600.0) -> bool:
    """splice 결과의 폭주 구간 검사. 한계는 원본 최장구간×1.5(적응형). (JS isWaypointListSane)"""
    if not route or len(route) < 2:
        return False
    limit = max(abs_floor_km, _max_segment_km(original) * 1.5)
    for i, w in enumerate(route):
        if not math.isfinite(w["lon"]) or not math.isfinite(w["lat"]):
            return False
        if w["lat"] < -90 or w["lat"] > 90 or w["lon"] < -180 or w["lon"] > 180:
            return False
        if i > 0 and _dist(route[i - 1], w) > limit:
            return False
    return True


# ─── RL 모델 로딩 (지연 import + route당 캐시) ──────────────────────────────
def _load_trainer(route_key: str, verbose: bool):
    """route별 RLTrainer 를 1회 로드해 캐시. RL 미설치/모델없음 → None."""
    global _RL_IMPORT_FAILED
    if _RL_IMPORT_FAILED:
        return None
    # 남극 항로(ROSS/PENINSULA)는 전용 모델 재학습 없이 북극 NSR 빙산회피 모델을
    # 재사용한다(빙역 회피 정책은 반구 무관 일반화). 입력 위협셀은 IceField 의
    # 절대위도 기반 high_threat_cells 가 남극 셀도 포함하므로 동작 정합.
    key = route_key if route_key in ("NSR", "NWP", "TSR") else "NSR"
    if key in _TRAINER_CACHE:
        return _TRAINER_CACHE[key]

    try:
        rl_dir = Path(__file__).resolve().parents[2] / "services" / "rl-pipeline"
        if str(rl_dir) not in sys.path:
            sys.path.insert(0, str(rl_dir))
        from modules.rl_trainer import RLTrainer  # type: ignore[import]
    except Exception as e:  # noqa: BLE001
        if verbose:
            _log(f"[RL-BAKE] RL 의존성 로드 불가 — 베이크 건너뜀 ({e})")
        _RL_IMPORT_FAILED = True
        return None

    try:
        trainer = RLTrainer(model_key=f"{key}_normal", fixed_route=key)
        if not trainer.agent.load():
            if verbose:
                _log(f"[RL-BAKE] 모델 가중치 로드 실패: {key}_normal — 베이크 건너뜀")
            _TRAINER_CACHE[key] = None
            return None
        if verbose:
            _log(f"[RL-BAKE] RL 모델 로드: {key}_normal")
        _TRAINER_CACHE[key] = trainer
        return trainer
    except Exception as e:  # noqa: BLE001
        if verbose:
            _log(f"[RL-BAKE] trainer 생성 실패 ({e}) — 베이크 건너뜀")
        _TRAINER_CACHE[key] = None
        return None


def _load_land_mask(verbose: bool):
    """splice 후 육지 재정합용 LandMask. 없으면 None(재정합 생략)."""
    try:
        from pipeline.icebreaker.land_mask import LandMask  # noqa: PLC0415
        return LandMask.load()
    except Exception as e:  # noqa: BLE001
        if verbose:
            _log(f"[RL-BAKE] 육지 마스크 없음 — 디투어 육지 재정합 생략 ({e})")
        return None


# ─── 위협 클러스터링 ────────────────────────────────────────────────────────
def _make_cluster(items: list[tuple[float, dict]]) -> dict:
    kms = [k for k, _ in items]
    cells = [c for _, c in items]
    return {
        "center_km": sum(kms) / len(kms),
        "cells": cells,
        "max_thick": max(c["thickness"] for c in cells),
        "max_conc": max(c["concentration"] for c in cells),
    }


def _cluster_threats(near: list[tuple[float, dict]], gap_km: float) -> list[dict]:
    """km 정렬된 (km, cell) 리스트를 gap_km 기준으로 클러스터링."""
    clusters: list[dict] = []
    cur: list[tuple[float, dict]] = []
    last_km = None
    for km, cell in near:
        if last_km is None or km - last_km <= gap_km:
            cur.append((km, cell))
        else:
            clusters.append(_make_cluster(cur))
            cur = [(km, cell)]
        last_km = km
    if cur:
        clusters.append(_make_cluster(cur))
    return clusters


def _berg_size_m(cell: dict) -> float:
    """두께 → 대리 빙산 대표 길이(m). RL infer 입력용."""
    return min(20000.0, 5000.0 + cell["thickness"] * 3000.0)


# ─── 메인: RL 빙산 회피 베이크 ──────────────────────────────────────────────
def apply_rl_iceberg_avoidance(
    route: list[Position],
    display_route: list[Position],
    field,
    route_key: str,
    ice_class_pc: str,
    ship_speed_knots: float,
    verbose: bool = False,
    seed: int = 20240601,
) -> tuple[list[Position], list[Position], dict]:
    """land-refined route 에 RL 빙산 회피 디투어를 splice.

    Returns (new_route, new_display, rl_meta).
      rl_meta = {"applied": bool, "method": "RL"|None,
                 "segments": [{"start_km","end_km","confidence","berg_count"}]}
    실패/RL미설치 시 (route, display_route, {"applied": False, ...}) 그대로 반환.
    """
    random.seed(seed)
    np.random.seed(seed)
    rl_meta: dict = {"applied": False, "method": None, "segments": []}

    if not route or len(route) < 3:
        return route, display_route, rl_meta

    trainer = _load_trainer(route_key, verbose)
    if trainer is None:
        return route, display_route, rl_meta

    # 1) 위협 셀 → route 전방버퍼 필터
    try:
        cells = field.high_threat_cells(min_thick=THREAT_MIN_THICK_M)
    except Exception as e:  # noqa: BLE001
        if verbose:
            _log(f"[RL-BAKE] 위협 셀 열거 실패 ({e})")
        return route, display_route, rl_meta

    cum = _cumulative_km(route)
    near: list[tuple[float, dict]] = []
    for c in cells:
        pt = cast("Position", {"lat": c["lat"], "lon": c["lon"]})
        if _cross_track_km(pt, route) <= THREAT_BUFFER_KM:
            near.append((_nearest_route_km(pt, route, cum), c))
    if not near:
        if verbose:
            _log("[RL-BAKE] 항로 버퍼 내 위협 없음 — 회피 없음")
        return route, display_route, rl_meta
    near.sort(key=lambda x: x[0])

    clusters = _cluster_threats(near, CLUSTER_GAP_KM)
    # 두께 상위 MAX_DETOURS 만 채택(과도한 디투어/추론 방지)
    clusters.sort(key=lambda cl: cl["max_thick"], reverse=True)
    clusters = sorted(clusters[:MAX_DETOURS], key=lambda cl: cl["center_km"])
    if verbose:
        _log(f"[RL-BAKE] 위협 클러스터 {len(clusters)}개 (버퍼 내 위협셀 {len(near)}개)")

    total_km = cum[-1]
    mask = _load_land_mask(verbose)

    # 2) 클러스터별 RL 추론 → 디투어 스펙 수집(원본 route 인덱스 기준)
    specs: list[dict] = []
    for cl in clusters:
        km_c = cl["center_km"]
        approach_km = max(0.0, km_c - APPROACH_MARGIN_KM)
        end_km = min(total_km, km_c + LOOKAHEAD_KM)
        ins_start = _idx_at_or_before(cum, approach_km)
        ins_end = _idx_at_or_after(cum, end_km)
        if ins_end <= ins_start + 1 or ins_end >= len(route):
            continue

        approach_pt = _position_at_km(route, cum, approach_km)
        ahead_pt = _position_at_km(route, cum, min(approach_km + 10.0, total_km))
        heading = bearing(approach_pt, ahead_pt)
        next_wp = _position_at_km(route, cum, end_km)

        # infer 입력 빙산은 두께 상위 MAX_BERGS_PER_INFER 개로 제한(학습 분포 정합).
        berg_cells = sorted(cl["cells"], key=lambda c: c["thickness"], reverse=True)[:MAX_BERGS_PER_INFER]
        icebergs = [
            {"lat": c["lat"], "lon": c["lon"], "length_m": _berg_size_m(c)}
            for c in berg_cells
        ]
        try:
            res = trainer.infer(
                ship_state={
                    "lat": approach_pt["lat"],
                    "lon": approach_pt["lon"],
                    "heading": heading,
                    "speed_knots": ship_speed_knots,
                    "ice_class": ice_class_pc,
                    "next_waypoint": {"lat": next_wp["lat"], "lon": next_wp["lon"]},
                },
                icebergs=icebergs,
                ice_data={"concentration": cl["max_conc"]},
                weather={"visibility_km": 10.0, "wave_height_m": 1.0},
            )
        except Exception as e:  # noqa: BLE001
            if verbose:
                _log(f"[RL-BAKE]   km={km_c:.0f} infer 예외 — 건너뜀 ({e})")
            continue

        conf = float(res.get("confidence", 0.0))
        if res.get("fallback") or conf < MIN_CONFIDENCE:
            if verbose:
                _log(f"[RL-BAKE]   km={km_c:.0f} 저신뢰(conf={conf:.2f}) — 디투어 폐기")
            continue

        raw = res.get("projected_path") or []
        detour: list[Position] = [
            cast("Position", {"lat": float(p["lat"]), "lon": float(p["lon"])})
            for p in raw if "lat" in p and "lon" in p
        ]
        if not is_projected_path_sane(detour, approach_pt):
            if verbose:
                _log(f"[RL-BAKE]   km={km_c:.0f} 투영경로 비정상 — 폐기")
            continue

        start_wp = route[ins_start]
        end_wp = route[ins_end]
        if not is_detour_forward(detour, start_wp, end_wp):
            if verbose:
                _log(f"[RL-BAKE]   km={km_c:.0f} 역주행 디투어 — 폐기")
            continue

        specs.append({
            "ins_start": ins_start,
            "ins_end": ins_end,
            "detour": detour,
            "confidence": conf,
            "berg_count": len(cl["cells"]),
            "geo_start": detour[0],
            "geo_end": detour[-1],
        })
        if verbose:
            _log(f"[RL-BAKE]   km={km_c:.0f} 디투어 채택(conf={conf:.2f}, "
                  f"점{len(detour)}개, 위협{len(cl['cells'])}개)")

    if not specs:
        return route, display_route, rl_meta

    # 비겹침 필터(인덱스 오름차순 greedy)
    specs.sort(key=lambda s: s["ins_start"])
    filtered: list[dict] = []
    last_end = -1
    for s in specs:
        if s["ins_start"] > last_end:
            filtered.append(s)
            last_end = s["ins_end"]
    specs = filtered

    # 3) 인덱스 내림차순으로 splice(상위 인덱스부터 → 하위 인덱스 불변)
    new_route: list[Position] = list(route)
    for s in sorted(specs, key=lambda s: s["ins_start"], reverse=True):
        new_route = (
            new_route[: s["ins_start"] + 1]
            + s["detour"]
            + new_route[s["ins_end"]:]
        )

    # 4) 육지 재정합(detour 가 육지 통과 가능)
    if mask is not None:
        try:
            from pipeline.icebreaker.land_mask import refine_route  # noqa: PLC0415
            new_route = refine_route(new_route, mask)
        except Exception as e:  # noqa: BLE001
            if verbose:
                _log(f"[RL-BAKE] 육지 재정합 실패 — 디투어 폐기, 원본 유지 ({e})")
            return route, display_route, rl_meta

    # 5) 최종 정합성 — 비정상이면 전체 폐기
    if not is_waypoint_list_sane(new_route, route):
        if verbose:
            _log("[RL-BAKE] 최종 경로 비정상 — 디투어 폐기, 원본 유지")
        return route, display_route, rl_meta

    # 6) 표시용 단순화 + 회피 세그먼트(최종 km 좌표)
    new_display: list[Position] = new_route
    if mask is not None:
        try:
            from pipeline.icebreaker.land_mask import simplify_route  # noqa: PLC0415
            new_display = simplify_route(new_route, mask)
        except Exception:  # noqa: BLE001
            new_display = new_route

    cum_final = _cumulative_km(new_route)
    segments = []
    for s in specs:
        start_km = _nearest_route_km(s["geo_start"], new_route, cum_final)
        end_km = _nearest_route_km(s["geo_end"], new_route, cum_final)
        if end_km <= start_km:
            end_km = min(cum_final[-1], start_km + APPROACH_MARGIN_KM)
        segments.append({
            "start_km": round(start_km, 2),
            "end_km": round(end_km, 2),
            "confidence": round(s["confidence"], 3),
            "berg_count": s["berg_count"],
        })
    segments.sort(key=lambda x: x["start_km"])

    rl_meta = {"applied": True, "method": "RL", "segments": segments}
    if verbose:
        _log(f"[RL-BAKE] 회피 적용 완료: {len(segments)}개 구간, "
              f"{len(route)}→{len(new_route)} waypoints")
    return new_route, new_display, rl_meta
