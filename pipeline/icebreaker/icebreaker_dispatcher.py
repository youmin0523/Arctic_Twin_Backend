"""
icebreaker_dispatcher.py
========================
쇄빙선 에스코트 시스템 — 호출 판정, 매칭, 상태 머신, 통과 두께 보강.

Standalone module — arctic_master_router.py와 같은 스타일 (no Pydantic, no pytest).

주요 구성 요소
--------------
  1. needs_icebreaker / needs_icebreaker_lookahead  — RIO 기반 호출 판정
  2. assign_icebreaker                              — 거리 기반 매칭
  3. should_transition_to_*                         — 상태 전이 판정(순수)
  4. effective_ice_thickness                        — 동행 시 유효 두께 감소
  5. dispatch_tick                                  — 2단계용 순수함수 스켈레톤

rio_at_point 콜백
-----------------
본 모듈은 공간상 임의 지점의 RIO를 조회하는 `rio_at_point: Position -> float`
콜백을 인자로 받는다. 2단계 시뮬 오케스트레이터에서는 arctic_master_router의
`ice_conditions` 스냅샷과 `calculate_rio`를 합성한 클로저로 주입될 예정:

    def make_rio_at_point(ice_field, ship_ice_class_pc):
        def rio_at_point(pos: Position) -> float:
            ic = ice_field.sample(pos["lat"], pos["lon"])  # → list[IceCondition]
            return calculate_rio(ship_ice_class_pc, ic)
        return rio_at_point

1단계(본 파일)는 모듈 단위 테스트에서만 콜백을 스텁으로 주입한다.

Usage
-----
    python -m pipeline.icebreaker.icebreaker_dispatcher
"""

from __future__ import annotations
import math
from typing import Callable, Literal, TypedDict, cast

from pipeline.arctic_master_router import RIV_TABLE
from pipeline.icebreaker.models import (
    Icebreaker,
    Position,
    INITIAL_ICEBREAKERS,
    arc_to_pc,
)
from pipeline.processors.iceberg_detector import DetectionPostprocessor


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 상수
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# POLARIS RIO 호출 임계값 — ice_class 상대값 정책
#
# 근거
# ----
# POLARIS RIV(Risk Index Value)는 빙급별로 빙종에 대한 위험 점수다. 각 PC의
# 최저 RIV는 "해당 빙급이 접할 수 있는 최악 빙종에 대한 점수 = 한계 능력".
# 일률 절대 임계값(예: RIO<-10)은 빙급별 RIV 스케일이 크게 다르기(-3 vs -20)
# 때문에 부적절. 상대 비율 방식이 정책적으로 타당.
#
# THRESHOLD_RATIO = 0.5 의 의미
#   본선의 이론적 한계 능력의 50%에 도달한 RIO → 단독항행 위험 임계
#
# 정책적 근거
#   POLARIS 가 RIO<0 을 'Elevated Operational Risk' 로 정의하는데, 빙급별
#   절대값(-3 vs -15)이 너무 달라 일률 임계값은 부적절. 상대값이 합리적.
# 운영 근거
#   Yamal LNG Arc7 LNG 운반선이 한겨울 NSR 에서 원자력 쇄빙선 에스코트를
#   1~2회 받는 실제 운영 패턴과 본 정책 시뮬 결과가 정성적으로 부합.
# ═══════════════════════════════════════════════════════════════════════════════

THRESHOLD_RATIO: float = 0.1
# 결정 이력
#   0.5 (초기): Arc4 가 Glacier 분류 셀에서만 호출되어 클래스 차별화 소실
#   0.3 (2차): Arc4 -2.7 / Arc7 -1.8 / Arc9 -0.9. 이론적으로 깔끔하지만
#              아라온 사전배치(Nome) 시나리오에서는 여전히 Arc4 의 첫 호출이
#              Laptev(77°N, Glacier 분류) 에서만 발생 → Nome-Laptev 2600km 라
#              Araon 이 못 따라감. "아라온 동부 NSR 커버" narrative 성립 불가.
#   0.1 (현재): Arc4 -0.9, Arc7 -0.6, Arc9 -0.3. Arc4 가 Chukchi Sea 의
#              Thick FY (RIO ≈ -1) 에서 호출되어 Nome-Chukchi ≈ 880km 로
#              아라온 합류 가능. Arc7/Arc9 는 PC3/PC4 의 Thick FY RIV 가
#              양수라 Chukchi 에서 거의 트리거 안 됨 → "강한 빙급선은
#              동부 NSR 에서 자력항행 가능" 으로 자연스럽게 차별화.
#
#   새 임계값:
#     Arc9 (PC3, min_riv=-3) → -0.3
#     Arc7 (PC4, min_riv=-6) → -0.6
#     Arc4 (PC7, min_riv=-9) → -0.9

# 각 PC 등급의 최저 RIV — arctic_master_router.RIV_TABLE 에서 import 시 계산
# (PC1=+1, PC2=-1, PC3=-3, PC4=-6, PC5=-20, PC6=-8, PC7=-9)
PC_MIN_RIV: dict[str, float] = {
    pc: float(min(rivs.values()))
    for pc, rivs in RIV_TABLE.items()
    if pc.startswith("PC")
}


def _min_riv_for_pc(pc: str) -> float:
    """PC 등급의 최저 RIV. 미매핑 시 DEFAULT 로 -1.0 사용(= 임계 0)."""
    return PC_MIN_RIV.get(pc, -1.0)


def threshold_for_ice_class(ice_class: str) -> float:
    """본선 ice_class 의 RIO 호출 임계값.

    min_riv × THRESHOLD_RATIO. PC1 처럼 min_riv 가 양수인 경우는
    실질적으로 '아무 빙질에서도 호출 안 함' 을 뜻하도록 0 으로 클램프.
    """
    pc = arc_to_pc(ice_class)
    min_riv = _min_riv_for_pc(pc)
    return min(0.0, min_riv * THRESHOLD_RATIO)


# ═══════════════════════════════════════════════════════════════════════════════
# 전방 스캔(lookahead) — 쇄빙선 ETA 기반 동적화
#
# 본선 앞 얼마나 먼 곳까지 위험을 미리 볼지는 가장 가까운 idle 쇄빙선이
# 도착하는 동안 본선이 전진할 거리에 안전 마진을 더한 값이 합리적.
# 너무 짧으면 호출이 늦어 합류 전에 위험 통과; 너무 길면 거짓 양성.
# ═══════════════════════════════════════════════════════════════════════════════

LOOKAHEAD_SAFETY_MARGIN_KM: float = 50.0
LOOKAHEAD_MIN_KM: float = 100.0
# 500 은 부산 출발 직후 원거리 쇄빙선을 과조기 호출하는 부작용이 있어 200 으로 축소.
# 200km 안에 위험이 보일 때 호출 → 가까운 idle 쇄빙선이 실제 합류 가능한 범위.
LOOKAHEAD_MAX_KM: float = 200.0

# ─── 합류 실패 판정 / 릴레이 ─────────────────────────────────────────────────
# 본선이 동행 합류 전 이동 중인 쇄빙선에서 멀어지는 경우, 해당 ib 를 해제하고
# 더 적절한 ib 를 재호출(릴레이). 실제 운영 시 동일 본선에 구간별로 다른
# 쇄빙선이 배정되는 패턴과 부합.
INTERCEPT_FAIL_HOURS: float = 5.0           # 이 시간 동안 거리 안 좁혀지면 실패
INTERCEPT_CLOSE_RATE_PER_HOUR: float = 0.02  # 시간당 최소 closing 비율 (2%/h)
# 시간 기반 공식을 dt 에 비례해 per-tick ratio 로 변환하므로 TEST_SIM 3 동일 dt
# 불변성 보장. dt=1.0h → 0.98, dt=0.5h → 0.99, dt=2.0h → 0.96
INTERCEPT_FAIL_TICKS: int = 5               # 백워드 호환 상수 (dt=1.0h 기준)
INTERCEPT_CLOSE_RATIO: float = 0.98         # 백워드 호환 상수 (dt=1.0h 기준)

# 상태 전이 경계값
RENDEZVOUS_RADIUS_KM: float = 2.0        # ≤ 이면 합류 판정
ESCORT_SAFE_LOOKAHEAD_KM: float = 50.0   # 전방 이 거리까지 안전하면 해제
RETURN_RADIUS_KM: float = 5.0            # released → idle 복귀 판정

# ICEBREAKER_EFFECTIVE_FACTOR = 0.7 의 근거:
#   (1) 쇄빙선 통과 후 채널은 저온 대기/해수 노출로 부분 재결빙
#   (2) 채널 폭(~30m)이 대형 상선 폭보다 근소하게 넓을 뿐 측면 압력 존재
#   (3) 실 운항 경험 기반 보수값 — 쇄빙 능력의 70%만 본선 유효 감소로 반영
ICEBREAKER_EFFECTIVE_FACTOR: float = 0.7

# 항해 단위 변환
NM_TO_KM: float = 1.852
EARTH_RADIUS_KM: float = 6371.0

# 에스코트 시 쇄빙선이 본선 앞에 정렬할 거리 (1해리)
ESCORT_LEAD_NM: float = 1.0
ESCORT_LEAD_KM: float = ESCORT_LEAD_NM * NM_TO_KM

# 1단계 INITIAL_ICEBREAKERS 의 position 은 각 쇄빙선의 모항 좌표이므로,
# released → idle 복귀 판정용 home base 매핑을 모듈 로드 시 고정 추출.
_HOME_POSITIONS: dict[str, Position] = {
    ib["id"]: dict(ib["position"]) for ib in INITIAL_ICEBREAKERS  # type: ignore[misc]
}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 거리 유틸 (기존 haversine_km 재사용)
#
# IcebergDetector.haversine_km 은 @staticmethod 이며 시그니처가
# (lon1, lat1, lon2, lat2) 순서임에 주의.
# ═══════════════════════════════════════════════════════════════════════════════


def _km_between(a: Position, b: Position) -> float:
    return DetectionPostprocessor.haversine_km(a["lon"], a["lat"], b["lon"], b["lat"])


def bearing(f: Position, t: Position) -> float:
    """초기 방위각 (true bearing, degrees 0-360). Great-circle 공식."""
    lat1 = math.radians(f["lat"])
    lat2 = math.radians(t["lat"])
    dlon = math.radians(t["lon"] - f["lon"])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_position(pos: Position, bearing_deg: float, distance_km: float) -> Position:
    """주어진 방위각/거리로 great-circle destination 계산."""
    lat1 = math.radians(pos["lat"])
    lon1 = math.radians(pos["lon"])
    brng = math.radians(bearing_deg)
    ang = distance_km / EARTH_RADIUS_KM
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    # 경도 -180..180 정규화
    lon2_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return {"lat": math.degrees(lat2), "lon": lon2_deg}


def move_toward(
    from_pos: Position, to_pos: Position, speed_knots: float, dt_hours: float
) -> Position:
    """speed_knots × dt_hours 거리만큼 목적지 방향으로 전진.

    남은 거리 ≤ step 이면 목적지로 스냅. Great-circle 사용.
    """
    remaining = _km_between(from_pos, to_pos)
    step_km = speed_knots * NM_TO_KM * dt_hours
    if step_km <= 0.0 or remaining == 0.0:
        return cast("Position", dict(from_pos))
    if step_km >= remaining:
        return cast("Position", dict(to_pos))
    brng = bearing(from_pos, to_pos)
    return offset_position(from_pos, brng, step_km)


def _route_forward_bearing(forward_route: list[Position]) -> float:
    """경로 진행 방향(첫 두 점의 bearing). 점이 하나뿐이면 0."""
    if len(forward_route) < 2:
        return 0.0
    return bearing(forward_route[0], forward_route[1])


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 호출 판정
# ═══════════════════════════════════════════════════════════════════════════════


def needs_icebreaker(rio_value: float, ice_class: str) -> bool:
    """현재 RIO가 본선 클래스 기준 위험 영역(threshold_for_ice_class)이면 True.

    임계값은 ice_class → PC → PC_MIN_RIV × THRESHOLD_RATIO(=0.5). 경계값은
    미포함 (strict <).
    """
    return rio_value < threshold_for_ice_class(ice_class)


def needs_icebreaker_lookahead(
    route_points: list[Position],
    rio_at_point: Callable[[Position], float],
    ice_class: str,
    distance_km: float = LOOKAHEAD_MIN_KM,
) -> bool:
    """현재 위치부터 distance_km 앞까지 경로 구간 중 한 지점이라도 위험하면 True.

    route_points[0] 은 본선 현재 위치로 간주. 인접 점 사이 haversine 거리를
    누적하며, 누적 거리가 distance_km 를 초과하는 지점까지만 스캔.
    """
    if not route_points:
        return False

    # 첫 점(현재 위치) 검사
    if needs_icebreaker(rio_at_point(route_points[0]), ice_class):
        return True

    accumulated = 0.0
    for i in range(1, len(route_points)):
        seg = _km_between(route_points[i - 1], route_points[i])
        accumulated += seg
        if accumulated > distance_km:
            break
        if needs_icebreaker(rio_at_point(route_points[i]), ice_class):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 3b. 동적 lookahead
# ═══════════════════════════════════════════════════════════════════════════════


def dynamic_lookahead_km(
    ship_position: Position,
    ship_speed_knots: float,
    icebreakers: list[Icebreaker],
) -> float:
    """가장 가까운 idle 쇄빙선 ETA 동안 본선 전진 거리 + 안전 마진.

    - idle 쇄빙선이 없으면 LOOKAHEAD_MAX_KM (모두 사용 중이면 최대한 미리 봄)
    - 결과는 [LOOKAHEAD_MIN_KM, LOOKAHEAD_MAX_KM] 범위로 클램프
    """
    idle = [ib for ib in icebreakers if ib["status"] == "idle"]
    if not idle:
        return LOOKAHEAD_MAX_KM

    nearest = min(
        idle, key=lambda ib: _km_between(ship_position, ib["position"])
    )
    dist_to_ib_km = _km_between(ship_position, nearest["position"])
    ib_speed_kmh = nearest["speed_knots"] * NM_TO_KM
    if ib_speed_kmh <= 0.0:
        return LOOKAHEAD_MAX_KM
    eta_h = dist_to_ib_km / ib_speed_kmh
    ship_progress_km = ship_speed_knots * NM_TO_KM * eta_h
    raw = ship_progress_km + LOOKAHEAD_SAFETY_MARGIN_KM
    return max(LOOKAHEAD_MIN_KM, min(LOOKAHEAD_MAX_KM, raw))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 매칭
# ═══════════════════════════════════════════════════════════════════════════════


def assign_icebreaker(
    ship_position: Position,
    icebreakers: list[Icebreaker],
) -> Icebreaker | None:
    """idle 상태 쇄빙선 중 최단거리 반환. 후보 없으면 None.

    동점 시 리스트 순서상 먼저 나온 쇄빙선을 반환 (결정적).
    """
    best: Icebreaker | None = None
    best_km = float("inf")
    for ib in icebreakers:
        if ib["status"] != "idle":
            continue
        d = _km_between(ship_position, ib["position"])
        if d < best_km:
            best_km = d
            best = ib
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 상태 전이 판정 (순수 함수 — side-effect 없음)
# ═══════════════════════════════════════════════════════════════════════════════


def should_transition_to_rendezvous(ib: Icebreaker, ship_position: Position) -> bool:
    """dispatched → rendezvous: 쇄빙선-본선 거리 ≤ RENDEZVOUS_RADIUS_KM.

    경계값(=)은 True(합류 성공)로 분류. 수치 오차 고려해 포함.
    """
    if ib["status"] != "dispatched":
        return False
    return _km_between(ib["position"], ship_position) <= RENDEZVOUS_RADIUS_KM


def should_transition_to_escorting(ib: Icebreaker, ship_position: Position) -> bool:
    """rendezvous → escorting: 본선이 합류 지점에 도달(동일 반경 이내)."""
    if ib["status"] != "rendezvous":
        return False
    return _km_between(ib["position"], ship_position) <= RENDEZVOUS_RADIUS_KM


def should_transition_to_released(
    ib: Icebreaker,
    ship_ice_class: str,
    forward_route: list[Position],
    rio_at_point: Callable[[Position], float],
) -> bool:
    """escorting → released: 전방 ESCORT_SAFE_LOOKAHEAD_KM 구간이 모두 안전.

    '안전'은 needs_icebreaker()의 보수 조건 — 즉 해당 구간에서 호출 필요 없음.
    """
    if ib["status"] != "escorting":
        return False
    return not needs_icebreaker_lookahead(
        forward_route,
        rio_at_point,
        ship_ice_class,
        distance_km=ESCORT_SAFE_LOOKAHEAD_KM,
    )


def should_transition_to_idle(ib: Icebreaker, base_position: Position) -> bool:
    """released → idle: 모항/복귀 지점 근처(≤ RETURN_RADIUS_KM) 도달."""
    if ib["status"] != "released":
        return False
    return _km_between(ib["position"], base_position) <= RETURN_RADIUS_KM


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 통과 두께 보강
# ═══════════════════════════════════════════════════════════════════════════════


def effective_ice_thickness(
    actual_thickness_m: float,
    escort: Icebreaker | None,
) -> float:
    """쇄빙선이 동행 중이면 유효 두께를 선형 감소시킴.

    - escort 가 None 이거나 status != 'escorting' → 원본 반환
    - 동행 중 → max(0, actual - breakable * ICEBREAKER_EFFECTIVE_FACTOR)
    """
    if escort is None or escort["status"] != "escorting":
        return actual_thickness_m
    reduction = escort["breakable_thickness_m"] * ICEBREAKER_EFFECTIVE_FACTOR
    return max(0.0, actual_thickness_m - reduction)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. dispatch_tick — 2단계 오케스트레이터 진입점 (스켈레톤)
# ═══════════════════════════════════════════════════════════════════════════════


DispatchEventType = Literal[
    "call", "rendezvous", "start_escort", "release", "return", "intercept_failed"
]


def _intercept_window_ticks(dt_hours: float) -> int:
    """INTERCEPT_FAIL_HOURS 시간 판정에 필요한 틱 수 (최소 2, ceil 보정)."""
    import math
    if dt_hours <= 0.0:
        return INTERCEPT_FAIL_TICKS
    return max(2, int(math.ceil(INTERCEPT_FAIL_HOURS / dt_hours)))


def is_failing_to_intercept(
    icebreaker: Icebreaker,
    ship_position: Position,
    intercept_history: dict[str, list[float]],
    dt_hours: float = 1.0,
) -> bool:
    """dispatched 쇄빙선이 본선과의 거리를 INTERCEPT_FAIL_HOURS 동안 좁히지
    못했으면 True (dt_hours 불변 — tick 수가 dt 에 비례 조정).

    - status != dispatched → False
    - history 길이 < window+1 → False
    - 마지막 window+1 거리 중 인접 쌍 모두에서 curr >= prev × 0.98 → True
    """
    del ship_position  # 시그니처 일관성용
    if icebreaker["status"] != "dispatched":
        return False
    window = _intercept_window_ticks(dt_hours)
    hist = intercept_history.get(icebreaker["id"], [])
    if len(hist) < window + 1:
        return False
    # per-tick ratio = 1 - RATE × dt_hours (dt 비례 스케일)
    per_tick_ratio = max(0.0, 1.0 - INTERCEPT_CLOSE_RATE_PER_HOUR * dt_hours)
    recent = hist[-(window + 1):]
    for i in range(1, len(recent)):
        if recent[i] < recent[i - 1] * per_tick_ratio:
            return False
    return True


class DispatchEvent(TypedDict):
    type: DispatchEventType
    icebreaker_id: str
    ship_id: str


def dispatch_tick(
    ship_id: str,
    ship_position: Position,
    ship_ice_class: str,
    ship_speed_knots: float,
    forward_route: list[Position],
    rio_at_point: Callable[[Position], float],
    icebreakers: list[Icebreaker],
    dt_hours: float = 1.0,
    intercept_history: dict[str, list[float]] | None = None,
) -> tuple[list[Icebreaker], list[DispatchEvent], dict[str, list[float]]]:
    """매 시뮬 스텝 호출되는 순수 함수.

    Processing order per tick:
      (0) dispatched ib 의 본선 거리 → intercept_history 업데이트
      (1) 현재 status 기반 위치 업데이트
      (2) 상태 전이 판정 → 이벤트 발행
      (3) 합류 실패 판정 → 실패 시 강제 release + intercept_failed 이벤트
      (4) 신규 호출 필요성 판정 → 매칭 → call 이벤트

    Returns (updated_icebreakers, events, updated_history). history 는 caller
    가 다음 틱에 다시 넘겨야 연속 판정이 가능하다 (모듈 전역 금지).
    """
    updated: list[Icebreaker] = [dict(ib) for ib in icebreakers]  # type: ignore[misc]
    for ib in updated:
        ib["position"] = cast("Position", dict(ib["position"]))
    events: list[DispatchEvent] = []

    # history 복사 (caller mutation 방지)
    history: dict[str, list[float]] = {
        k: list(v) for k, v in (intercept_history or {}).items()
    }

    # ── (0) 이동 전 거리 스냅샷 → history append (dispatched 만) ─────────
    history_max = _intercept_window_ticks(dt_hours) + 1
    for ib in updated:
        if ib["status"] == "dispatched":
            d = _km_between(ib["position"], ship_position)
            lst = history.get(ib["id"], [])
            lst = list(lst)
            lst.append(d)
            if len(lst) > history_max:
                lst = lst[-history_max:]
            history[ib["id"]] = lst
        else:
            # non-dispatched 는 history 리셋 (다시 dispatched 되면 새로 시작)
            history.pop(ib["id"], None)

    # ── (1) 이동 ──────────────────────────────────────────────────────────
    for ib in updated:
        s = ib["status"]
        if s == "dispatched":
            ib["position"] = move_toward(
                ib["position"], ship_position, ib["speed_knots"], dt_hours
            )
        elif s in ("rendezvous", "escorting"):
            brng = _route_forward_bearing(forward_route)
            ib["position"] = offset_position(ship_position, brng, ESCORT_LEAD_KM)
        elif s == "released":
            home = _HOME_POSITIONS.get(ib["id"], ib["position"])
            ib["position"] = move_toward(
                ib["position"], home, ib["speed_knots"], dt_hours
            )
        # idle → 정지

    # ── (2) 전이 판정 ─────────────────────────────────────────────────────
    for ib in updated:
        s = ib["status"]
        if s == "dispatched" and should_transition_to_rendezvous(ib, ship_position):
            ib["status"] = "rendezvous"
            events.append(
                {"type": "rendezvous", "icebreaker_id": ib["id"], "ship_id": ship_id}
            )
            history.pop(ib["id"], None)
        elif s == "rendezvous" and should_transition_to_escorting(ib, ship_position):
            ib["status"] = "escorting"
            events.append(
                {"type": "start_escort", "icebreaker_id": ib["id"], "ship_id": ship_id}
            )
        elif s == "escorting" and should_transition_to_released(
            ib, ship_ice_class, forward_route, rio_at_point
        ):
            ib["status"] = "released"
            ib["escorting_ship_id"] = None
            events.append(
                {"type": "release", "icebreaker_id": ib["id"], "ship_id": ship_id}
            )
        elif s == "released" and should_transition_to_idle(
            ib, _HOME_POSITIONS.get(ib["id"], ib["position"])
        ):
            ib["status"] = "idle"
            events.append(
                {"type": "return", "icebreaker_id": ib["id"], "ship_id": ship_id}
            )

    # ── (3) 합류 실패 판정 + (4) 신규 호출 ────────────────────────────────
    assigned_ib: Icebreaker | None = next(
        (
            ib for ib in updated
            if ib.get("escorting_ship_id") == ship_id
            and ib["status"] in ("dispatched", "rendezvous", "escorting")
        ),
        None,
    )

    if assigned_ib is not None:
        if is_failing_to_intercept(assigned_ib, ship_position, history, dt_hours):
            # 릴레이 발동: 현재 ib 를 released 로 강제 + 신규 호출 허용
            assigned_ib["status"] = "released"
            assigned_ib["escorting_ship_id"] = None
            events.append(
                {
                    "type": "intercept_failed",
                    "icebreaker_id": assigned_ib["id"],
                    "ship_id": ship_id,
                }
            )
            history.pop(assigned_ib["id"], None)
            assigned_ib = None
        else:
            return updated, events, history

    dyn_km = dynamic_lookahead_km(ship_position, ship_speed_knots, updated)
    if not needs_icebreaker_lookahead(
        forward_route, rio_at_point, ship_ice_class, distance_km=dyn_km
    ):
        return updated, events, history

    chosen = assign_icebreaker(ship_position, updated)
    if chosen is None:
        return updated, events, history

    for ib in updated:
        if ib["id"] == chosen["id"]:
            ib["status"] = "dispatched"
            ib["escorting_ship_id"] = ship_id
            break

    events.append(
        {"type": "call", "icebreaker_id": chosen["id"], "ship_id": ship_id}
    )
    return updated, events, history


# ═══════════════════════════════════════════════════════════════════════════════
# 8. BUILT-IN TEST SUITE
# ═══════════════════════════════════════════════════════════════════════════════


def _separator(title: str) -> None:
    width = 72
    print()
    print("=" * width)
    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    print(f"  {safe_title}")
    print("=" * width)


def _ship_pos(lat: float, lon: float) -> Position:
    return {"lat": lat, "lon": lon}


def run_tests() -> None:
    """쇄빙선 에스코트 모듈 검증 — 7개 테스트 그룹."""

    # ─────────────────────────────────────────────────────────────────────
    # TEST 1 — needs_icebreaker (경계값 포함)
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 1 - needs_icebreaker (relative ice_class threshold)")

    # PC_MIN_RIV × 0.1 기준: PC3=-0.3, PC4=-0.6, PC7=-0.9
    assert abs(threshold_for_ice_class("Arc9") - (-0.3)) < 1e-6
    assert abs(threshold_for_ice_class("Arc7") - (-0.6)) < 1e-6
    assert abs(threshold_for_ice_class("Arc4") - (-0.9)) < 1e-6

    # 각 클래스 양쪽 명확한 값으로 검증
    assert needs_icebreaker(-0.25, "Arc9") is False
    assert needs_icebreaker(-0.40, "Arc9") is True
    assert needs_icebreaker(-0.55, "Arc7") is False
    assert needs_icebreaker(-0.70, "Arc7") is True
    assert needs_icebreaker(-0.85, "Arc4") is False
    assert needs_icebreaker(-1.00, "Arc4") is True

    print(f"  thresholds: Arc9={threshold_for_ice_class('Arc9'):+.1f}, "
          f"Arc7={threshold_for_ice_class('Arc7'):+.1f}, "
          f"Arc4={threshold_for_ice_class('Arc4'):+.1f}")
    print("  [PASS] needs_icebreaker relative threshold (ratio=0.3)")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 2 — needs_icebreaker_lookahead
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 2 - needs_icebreaker_lookahead (100km forward scan)")

    # Kara Sea ~ 경도 1도 ≈ 40km @ 75°N, 위도 1도 ≈ 111km
    # 위도를 0.5°씩 증가 → 약 55km 간격
    base_lat, base_lon = 75.0, 80.0
    route = [
        _ship_pos(base_lat + 0.0, base_lon),        # 0 km
        _ship_pos(base_lat + 0.5, base_lon),        # ~55 km
        _ship_pos(base_lat + 1.0, base_lon),        # ~111 km (> 100)
        _ship_pos(base_lat + 1.5, base_lon),        # ~166 km
    ]

    def rio_safe(_p: Position) -> float:
        return 1.5

    def rio_bad_at_index_1(p: Position) -> float:
        return -12.0 if abs(p["lat"] - (base_lat + 0.5)) < 1e-6 else 1.5

    def rio_bad_at_index_3(p: Position) -> float:
        return -12.0 if abs(p["lat"] - (base_lat + 1.5)) < 1e-6 else 1.5

    assert needs_icebreaker_lookahead(route, rio_safe, "Arc9") is False
    assert needs_icebreaker_lookahead(route, rio_bad_at_index_1, "Arc9") is True
    # index 3 은 누적 ~166km 지점 → lookahead 100 초과 → 스캔 안 함
    assert needs_icebreaker_lookahead(route, rio_bad_at_index_3, "Arc9") is False
    assert needs_icebreaker_lookahead([], rio_safe, "Arc9") is False
    print("  [PASS] lookahead: safe / near-hazard / beyond-range / empty")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 3 — assign_icebreaker
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 3 - assign_icebreaker (nearest idle)")

    ship_kara = _ship_pos(75.0, 80.0)  # Kara Sea
    chosen = assign_icebreaker(ship_kara, INITIAL_ICEBREAKERS)
    assert chosen is not None
    # 단일 함대(아라온) — 항상 본인이 최단
    assert chosen["id"] == "ib-araon"
    dist_km = _km_between(ship_kara, chosen["position"])
    print(f"  nearest to Kara Sea (75N, 80E): {chosen['name_ko']} "
          f"({dist_km:.0f} km from 인천)")

    # 전원 dispatched → None
    all_busy = [dict(ib) for ib in INITIAL_ICEBREAKERS]
    for ib in all_busy:
        ib["status"] = "dispatched"
    assert assign_icebreaker(ship_kara, all_busy) is None  # type: ignore[arg-type]

    # 빈 리스트 → None
    assert assign_icebreaker(ship_kara, []) is None
    print("  [PASS] nearest / all-busy / empty")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 4 — 상태 전이 (경계값 명시)
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 4 - state transitions (boundary cases)")

    # 동일 경도에서 위도 차이로 거리 조절 — 1° ≈ 111 km
    ib_base: Icebreaker = {
        "id": "ib-test",
        "name_ko": "TEST",
        "position": {"lat": 75.0, "lon": 80.0},
        "home_port": "X",
        "status": "dispatched",
        "speed_knots": 20.0,
        "ice_class": "Arc9",
        "breakable_thickness_m": 2.8,
        "escorting_ship_id": "ship-1",
    }

    # RENDEZVOUS_RADIUS_KM = 2.0 — 경계값 포함 (≤) 규약 검증
    # 위도 1° == π*R/180 km (R=6371). 경도 동일, 위도만 차이 → haversine 정확값.
    import math
    KM_PER_DEG_LAT = math.pi * 6371.0 / 180.0  # ≈ 111.1949
    def _lat_offset_for_km(km: float) -> float:
        return km / KM_PER_DEG_LAT
    # float precision 고려해 경계 내부/외부를 각각 약간 안쪽/바깥쪽으로 이동
    ship_at_1_9999km = _ship_pos(75.0 + _lat_offset_for_km(1.9999), 80.0)
    ship_at_2_0001km = _ship_pos(75.0 + _lat_offset_for_km(2.0001), 80.0)
    ship_at_1_5km = _ship_pos(75.0 + _lat_offset_for_km(1.5), 80.0)
    ship_at_5km = _ship_pos(75.0 + _lat_offset_for_km(5.0), 80.0)

    assert should_transition_to_rendezvous(ib_base, ship_at_1_5km) is True
    assert should_transition_to_rendezvous(ib_base, ship_at_5km) is False
    # 경계 INCLUSIVE (≤) 규약: 1.9999km → True, 2.0001km → False
    assert should_transition_to_rendezvous(ib_base, ship_at_1_9999km) is True, \
        "just inside 2.0km boundary should be True (<=)"
    assert should_transition_to_rendezvous(ib_base, ship_at_2_0001km) is False, \
        "just outside 2.0km boundary should be False"
    # status 가 맞지 않으면 False
    ib_idle = dict(ib_base); ib_idle["status"] = "idle"
    assert should_transition_to_rendezvous(ib_idle, ship_at_1_5km) is False  # type: ignore[arg-type]
    print("  [PASS] dispatched -> rendezvous (incl. 2.0km boundary inclusive)")

    # escorting → released: 전방 50km 안전 판정
    ib_escorting = dict(ib_base)
    ib_escorting["status"] = "escorting"

    # 경로: ~55km 간격 두 점 → 첫 점(현재) + 50km 이내 모두 커버
    fwd_safe = [
        _ship_pos(75.0, 80.0),
        _ship_pos(75.0 + 0.5, 80.0),   # ~55 km (lookahead 50 초과 — 스캔 제외)
    ]
    assert should_transition_to_released(
        ib_escorting, "Arc9", fwd_safe, rio_safe  # type: ignore[arg-type]
    ) is True

    # 전방 ~30km 에 위험 지점
    hazard_30_lat = 75.0 + _lat_offset_for_km(30.0)
    fwd_hazard_near = [
        _ship_pos(75.0, 80.0),
        _ship_pos(hazard_30_lat, 80.0),
        _ship_pos(75.0 + 0.5, 80.0),
    ]
    def rio_bad_at_30(p: Position) -> float:
        return -12.0 if abs(p["lat"] - hazard_30_lat) < 1e-9 else 1.5
    assert should_transition_to_released(
        ib_escorting, "Arc9", fwd_hazard_near, rio_bad_at_30  # type: ignore[arg-type]
    ) is False

    # ESCORT_SAFE_LOOKAHEAD_KM 경계 INCLUSIVE (≤ 50.0) 검증
    # lookahead 구현은 `누적>distance` 면 break → 누적≤50 이면 검사 포함.
    # 49.9999km 지점 위험 → False(released 아님), 50.0001km 지점 위험 → True(released)
    hazard_just_inside_lat = 75.0 + _lat_offset_for_km(49.9999)
    hazard_just_outside_lat = 75.0 + _lat_offset_for_km(50.0001)
    fwd_hazard_just_inside = [
        _ship_pos(75.0, 80.0),
        _ship_pos(hazard_just_inside_lat, 80.0),
    ]
    fwd_hazard_just_outside = [
        _ship_pos(75.0, 80.0),
        _ship_pos(hazard_just_outside_lat, 80.0),
    ]
    def rio_bad_inside(p: Position) -> float:
        return -12.0 if abs(p["lat"] - hazard_just_inside_lat) < 1e-9 else 1.5
    def rio_bad_outside(p: Position) -> float:
        return -12.0 if abs(p["lat"] - hazard_just_outside_lat) < 1e-9 else 1.5
    assert should_transition_to_released(
        ib_escorting, "Arc9", fwd_hazard_just_inside, rio_bad_inside  # type: ignore[arg-type]
    ) is False, "hazard just inside 50km should prevent release"
    assert should_transition_to_released(
        ib_escorting, "Arc9", fwd_hazard_just_outside, rio_bad_outside  # type: ignore[arg-type]
    ) is True, "hazard beyond 50km should NOT prevent release"
    print("  [PASS] escorting -> released (incl. 50km boundary inclusive)")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 5 — effective_ice_thickness
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 5 - effective_ice_thickness")

    assert effective_ice_thickness(3.0, None) == 3.0

    escort_on: Icebreaker = dict(ib_base)  # type: ignore[assignment]
    escort_on["status"] = "escorting"
    escort_on["breakable_thickness_m"] = 2.8
    # 3.0 - 2.8*0.7 = 3.0 - 1.96 = 1.04
    eff = effective_ice_thickness(3.0, escort_on)
    assert abs(eff - 1.04) < 1e-9, f"expected 1.04, got {eff}"

    escort_dispatched: Icebreaker = dict(ib_base)  # type: ignore[assignment]
    escort_dispatched["status"] = "dispatched"
    assert effective_ice_thickness(3.0, escort_dispatched) == 3.0

    # actual < 감소량 → 0 clamp
    assert effective_ice_thickness(1.0, escort_on) == 0.0
    print("  [PASS] None / escorting / dispatched / clamp-to-zero")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 6 — 모델 / 초기 함대 sanity
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 6 - initial fleet sanity")

    assert len(INITIAL_ICEBREAKERS) == 1, "expect only Araon in Korean fleet"
    assert INITIAL_ICEBREAKERS[0]["id"] == "ib-araon"
    for ib in INITIAL_ICEBREAKERS:
        assert ib["status"] == "idle"
        assert -90.0 <= ib["position"]["lat"] <= 90.0
        assert -180.0 <= ib["position"]["lon"] <= 180.0
        assert ib["breakable_thickness_m"] > 0.0
        assert ib["escorting_ship_id"] is None
    # Arc → PC 매핑 확인
    assert arc_to_pc("Arc9") == "PC3"
    assert arc_to_pc("Arc7") == "PC4"
    assert arc_to_pc("Unknown") == "Unknown"  # passthrough
    print(f"  fleet size = {len(INITIAL_ICEBREAKERS)}, all idle, "
          f"coords valid")
    print("  [PASS] initial fleet + arc_to_pc mapping")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 7 — dispatch_tick 통합 검증
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 7 - dispatch_tick integration")

    ship_pos = _ship_pos(75.0, 80.0)
    # 전방 ~55km 에 위험 (Arc9 기준 -12 → -10 미만)
    forward = [
        _ship_pos(75.0, 80.0),
        _ship_pos(75.5, 80.0),
    ]
    def rio_hazard_fwd(p: Position) -> float:
        return -12.0 if p["lat"] > 75.3 else 1.5

    fleet = [dict(ib) for ib in INITIAL_ICEBREAKERS]
    updated, events, hist = dispatch_tick(
        ship_id="ship-1",
        ship_position=ship_pos,
        ship_ice_class="Arc9",
        ship_speed_knots=15.0,
        forward_route=forward,
        rio_at_point=rio_hazard_fwd,
        icebreakers=fleet,  # type: ignore[arg-type]
    )

    assert len(events) == 1, f"expected 1 call event, got {len(events)}"
    assert events[0]["type"] == "call"
    assert events[0]["ship_id"] == "ship-1"

    dispatched = [ib for ib in updated if ib["status"] == "dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["id"] == events[0]["icebreaker_id"]
    assert dispatched[0]["id"] == "ib-araon"
    assert dispatched[0]["escorting_ship_id"] == "ship-1"

    # 원본 fleet 는 변경되지 않음 (순수성)
    assert all(ib["status"] == "idle" for ib in INITIAL_ICEBREAKERS)
    print(f"  dispatched: {dispatched[0]['name_ko']} -> ship-1")

    # 동일 본선 재호출 방지: 이미 assigned 상태면 이벤트 0개
    updated2, events2, hist2 = dispatch_tick(
        ship_id="ship-1",
        ship_position=ship_pos,
        ship_ice_class="Arc9",
        ship_speed_knots=15.0,
        forward_route=forward,
        rio_at_point=rio_hazard_fwd,
        icebreakers=updated,
        intercept_history=hist,
    )
    assert events2 == [], "should not re-call for same ship already assigned"

    # 안전 상황 → 이벤트 0개
    fleet3 = [dict(ib) for ib in INITIAL_ICEBREAKERS]
    _, events3, _ = dispatch_tick(
        ship_id="ship-2",
        ship_position=ship_pos,
        ship_ice_class="Arc9",
        ship_speed_knots=15.0,
        forward_route=forward,
        rio_at_point=lambda _p: 1.5,
        icebreakers=fleet3,  # type: ignore[arg-type]
    )
    assert events3 == []
    print("  [PASS] call event / status update / no duplicate / safe noop")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 8 — move_toward 정확도
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 8 - move_toward accuracy (10kn * 1h = 18.52km)")

    start = _ship_pos(70.0, 30.0)
    target = _ship_pos(80.0, 30.0)  # 정북 ~1110km
    moved = move_toward(start, target, speed_knots=10.0, dt_hours=1.0)
    traveled = _km_between(start, moved)
    expected = 10.0 * NM_TO_KM * 1.0  # 18.52 km
    rel_err = abs(traveled - expected) / expected
    assert rel_err < 0.01, f"move_toward err={rel_err:.4f}, traveled={traveled}"
    print(f"  traveled={traveled:.4f}km, expected={expected:.4f}km, rel_err={rel_err*100:.3f}%")

    # 남은 거리가 step 보다 작으면 목적지로 스냅
    close_target = _ship_pos(70.05, 30.0)  # ~5.56km
    snapped = move_toward(start, close_target, speed_knots=10.0, dt_hours=1.0)
    assert snapped == close_target, "should snap to target when step >= remaining"
    print("  [PASS] distance accuracy & snap-to-target")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 9 — bearing / offset_position 왕복
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 9 - bearing / offset_position round-trip")

    origin = _ship_pos(75.0, 80.0)
    for brng in (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0):
        dest = offset_position(origin, brng, 50.0)
        # 실제 이동 거리 검증
        d = _km_between(origin, dest)
        assert abs(d - 50.0) < 0.01, f"offset dist err at brng={brng}: {d}"
        # 방위각 왕복
        back = bearing(origin, dest)
        diff = min(abs(back - brng), 360.0 - abs(back - brng))
        assert diff < 0.5, f"bearing round-trip err at brng={brng}: got {back}, diff={diff}"
    print("  [PASS] 8 cardinal+intercardinal bearings round-trip (<0.5 deg)")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 10 — dispatch_tick 풀 사이클 (call->rdv->escort->release->return->idle)
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 10 - dispatch_tick full cycle")

    # 합성 쇄빙선 1척 (고속) — 짧은 거리 시나리오로 테스트 시간 단축
    synth_home: Position = {"lat": 75.0, "lon": 78.0}
    synth: Icebreaker = {
        "id": "ib-synth",
        "name_ko": "SYNTH",
        "position": cast("Position", dict(synth_home)),
        "home_port": "TEST",
        "status": "idle",
        "speed_knots": 60.0,   # ~111 km/h
        "ice_class": "Arc9",
        "breakable_thickness_m": 2.5,
        "escorting_ship_id": None,
    }
    # 테스트용 home_positions 주입 (모듈 _HOME_POSITIONS 에 합성 쇄빙선 등록)
    _HOME_POSITIONS["ib-synth"] = cast("Position", dict(synth_home))

    ship_pos_t10 = _ship_pos(75.0, 80.0)  # 쇄빙선 기준 동쪽 ~57km
    forward_t10 = [
        _ship_pos(75.0, 80.0),
        _ship_pos(75.0, 80.5),   # ~14km 동진
        _ship_pos(75.0, 81.0),
    ]

    # 처음 15틱은 위험, 이후 안전
    hazard_ticks = [0]
    def rio_toggle(_p: Position) -> float:
        return -12.0 if hazard_ticks[0] < 15 else 2.0

    fleet_t10: list[Icebreaker] = [synth]  # type: ignore[list-item]
    seen_events: list[str] = []
    final_status = None
    hist_t10: dict[str, list[float]] = {}
    for tick in range(200):
        hazard_ticks[0] = tick
        fleet_t10, events_t10, hist_t10 = dispatch_tick(
            ship_id="ship-cycle",
            ship_position=ship_pos_t10,
            ship_ice_class="Arc9",
            ship_speed_knots=15.0,
            forward_route=forward_t10,
            rio_at_point=rio_toggle,
            icebreakers=fleet_t10,
            dt_hours=1.0,
            intercept_history=hist_t10,
        )
        for ev in events_t10:
            seen_events.append(ev["type"])
        final_status = fleet_t10[0]["status"]
        if final_status == "idle" and len(seen_events) >= 5:
            break

    required = ["call", "rendezvous", "start_escort", "release", "return"]
    for r in required:
        assert r in seen_events, f"missing event '{r}' in cycle: {seen_events}"
    assert final_status == "idle", f"final status expected idle, got {final_status}"
    print(f"  events seen in order: {seen_events}")
    print(f"  final status: {final_status}, ticks={tick+1}")
    print("  [PASS] full cycle call->rdv->escort->release->return->idle")

    # 합성 엔트리 정리 (regression 안전성)
    _HOME_POSITIONS.pop("ib-synth", None)

    # ─────────────────────────────────────────────────────────────────────
    # TEST 11 — dynamic_lookahead_km
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 11 - dynamic_lookahead_km")

    def _synth_ib(id_: str, lat: float, lon: float, speed: float,
                  status: str = "idle") -> Icebreaker:
        return cast("Icebreaker", {
            "id": id_, "name_ko": id_, "position": {"lat": lat, "lon": lon},
            "home_port": "T", "status": status, "speed_knots": speed,
            "ice_class": "Arc9", "breakable_thickness_m": 2.5,
            "escorting_ship_id": None,
        })

    ship = _ship_pos(75.0, 80.0)
    # 가장 가까운 idle 이 ~100km, 본선 15kn, 쇄빙선 20kn
    # ETA ≈ 100/(20*1.852) = 2.6998h, 본선 진행 15*1.852*2.6998 ≈ 75 km
    # 원시값 = 75 + 50(마진) = 125 km → MIN..MAX 내 → 125 그대로
    lat_offset = 100.0 / (3.141592653589793 * 6371.0 / 180.0)
    fleet_a = [
        _synth_ib("near100", 75.0 + lat_offset, 80.0, 20.0),
        _synth_ib("far300", 75.0 + lat_offset * 3, 80.0, 20.0),
    ]
    dyn_a = dynamic_lookahead_km(ship, 15.0, fleet_a)  # type: ignore[arg-type]
    assert abs(dyn_a - 125.0) < 1.0, \
        f"expected ~125 km, got {dyn_a:.2f}"
    print(f"  case A (near 100km idle, ship 15kn, ib 20kn): {dyn_a:.2f} km")

    # 모두 바쁨 → LOOKAHEAD_MAX_KM (현재 200)
    fleet_b = [_synth_ib("busy", 75.1, 80.0, 20.0, status="dispatched")]
    dyn_b = dynamic_lookahead_km(ship, 15.0, fleet_b)  # type: ignore[arg-type]
    assert dyn_b == LOOKAHEAD_MAX_KM == 200.0, \
        f"all busy should return MAX=200, got {dyn_b}"
    print(f"  case B (all busy): {dyn_b:.0f} km (= MAX {LOOKAHEAD_MAX_KM:.0f})")

    # 가까운 쇄빙선 10km → 계산값 < MIN → 클램프 MIN
    lat_10km = 10.0 / (3.141592653589793 * 6371.0 / 180.0)
    fleet_c = [_synth_ib("near10", 75.0 + lat_10km, 80.0, 20.0)]
    dyn_c = dynamic_lookahead_km(ship, 15.0, fleet_c)  # type: ignore[arg-type]
    assert dyn_c == LOOKAHEAD_MIN_KM, \
        f"clamp to MIN expected, got {dyn_c}"
    print(f"  case C (near 10km): {dyn_c:.0f} km (= MIN clamped)")

    # idle 리스트 비어 있음 (fleet 자체가 빔) → MAX
    dyn_d = dynamic_lookahead_km(ship, 15.0, [])
    assert dyn_d == LOOKAHEAD_MAX_KM == 200.0
    print(f"  case D (empty fleet): {dyn_d:.0f} km (= MAX)")

    print("  [PASS] dynamic_lookahead_km: near / busy / clamp / empty")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 12 — intercept_failed 릴레이
    # ─────────────────────────────────────────────────────────────────────
    _separator("TEST 12 - intercept_failed + relay dispatch")

    # 본선이 매우 빠름(60kn), 쇄빙선은 느림(10kn) → 쫓아갈 수 없음
    # 쇄빙선 A 를 본선에서 서쪽 200km 에 배치, 본선은 동쪽으로 계속 이동
    ib_a_home: Position = {"lat": 75.0, "lon": 76.0}   # ~200km west of start
    ib_b_home: Position = {"lat": 75.0, "lon": 84.0}   # ~115km east — 더 가까움
    ib_a: Icebreaker = {
        "id": "ib-a", "name_ko": "A",
        "position": cast("Position", dict(ib_a_home)), "home_port": "AX",
        "status": "idle", "speed_knots": 10.0,
        "ice_class": "Arc9", "breakable_thickness_m": 2.0,
        "escorting_ship_id": None,
    }
    ib_b: Icebreaker = {
        "id": "ib-b", "name_ko": "B",
        "position": cast("Position", dict(ib_b_home)), "home_port": "BX",
        "status": "idle", "speed_knots": 20.0,
        "ice_class": "Arc9", "breakable_thickness_m": 2.0,
        "escorting_ship_id": None,
    }
    _HOME_POSITIONS["ib-a"] = cast("Position", dict(ib_a_home))
    _HOME_POSITIONS["ib-b"] = cast("Position", dict(ib_b_home))

    # 본선 40kn(고속), 동쪽으로 계속 이동하며 RIO=-5(위험) 유지
    ship_state: Position = {"lat": 75.0, "lon": 80.0}

    def rio_always_bad(_p: Position) -> float:
        return -5.0

    def forward_east(pos: Position) -> list[Position]:
        return [cast("Position", dict(pos)),
                {"lat": pos["lat"], "lon": pos["lon"] + 1.0}]

    fleet_t12: list[Icebreaker] = [ib_a, ib_b]  # type: ignore[list-item]
    hist_t12: dict[str, list[float]] = {}
    seen_t12: list[tuple[float, str, str]] = []
    first_call_id: str | None = None

    for tick in range(30):
        fwd = forward_east(ship_state)
        fleet_t12, events_t12, hist_t12 = dispatch_tick(
            ship_id="ship-fast",
            ship_position=dict(ship_state),  # type: ignore[arg-type]
            ship_ice_class="Arc9",
            ship_speed_knots=40.0,
            forward_route=fwd,
            rio_at_point=rio_always_bad,
            icebreakers=fleet_t12,
            dt_hours=1.0,
            intercept_history=hist_t12,
        )
        for ev in events_t12:
            seen_t12.append((float(tick), ev["type"], ev["icebreaker_id"]))
            if ev["type"] == "call" and first_call_id is None:
                first_call_id = ev["icebreaker_id"]

        # 본선 위치 갱신: 정동(90°) 40kn × 1h
        ship_state = offset_position(
            ship_state, 90.0, 40.0 * NM_TO_KM * 1.0  # type: ignore[arg-type]
        )

    event_types = [t for (_tk, t, _id) in seen_t12]
    print(f"  events: {seen_t12[:10]}")
    assert first_call_id is not None, "no initial call fired"
    assert "intercept_failed" in event_types, \
        f"expected intercept_failed, got {event_types}"
    # 첫 call 후 intercept_failed → 두 번째 call
    failed_idx = event_types.index("intercept_failed")
    post_fail_calls = [
        e for e in event_types[failed_idx + 1:] if e == "call"
    ]
    assert post_fail_calls, \
        f"expected new call after intercept_failed, got {event_types}"
    print(f"  first call: {first_call_id}, then intercept_failed + relay call")
    print("  [PASS] intercept_failed releases stale ib and dispatches new one")

    _HOME_POSITIONS.pop("ib-a", None)
    _HOME_POSITIONS.pop("ib-b", None)

    print()
    print("=" * 72)
    print("  ALL ICEBREAKER TESTS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    run_tests()
