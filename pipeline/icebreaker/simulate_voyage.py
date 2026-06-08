"""
simulate_voyage.py
==================
본선 1척을 주어진 경로(NSR/NWP/TSR/SUEZ/CAPE)를 따라 시간 스텝으로
진행시키면서 쇄빙선 에스코트 시스템을 시뮬레이션한다.

출력
----
- trace JSON : backend/data/simulations/*.json (Cesium 시각화용)
- 콘솔 타임라인 : 이벤트 발생 시점만 ASCII 토큰으로 출력

실행
----
    python -m pipeline.icebreaker.simulate_voyage          # 기본 NSR 3월 Arc7
    python -m pipeline.icebreaker.simulate_voyage --test   # run_tests 실행
"""

from __future__ import annotations
import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, cast

from pipeline.arctic_master_router import calculate_rio
from pipeline.icebreaker.ice_type_mapper import IceField
from pipeline.icebreaker.icebreaker_dispatcher import (
    LOOKAHEAD_MAX_KM,
    NM_TO_KM,
    DispatchEvent,
    _km_between,
    bearing,
    dispatch_tick,
    effective_ice_thickness,
    offset_position,
)
from pipeline.icebreaker.models import (
    FLEET_BY_ROUTE,
    Icebreaker,
    Position,
    arc_to_pc,
    fleet_for_route,
)
from pipeline.icebreaker.rl_iceberg_bake import apply_rl_iceberg_avoidance
from pipeline.icebreaker.routes_loader import load_routes


# 출력 디렉토리
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = _BACKEND_ROOT / "data" / "simulations"


# ─── 경로 진행 유틸 ─────────────────────────────────────────────────────────


def _segment_distances_km(route: list[Position]) -> list[float]:
    """경로 각 구간 길이. 길이 = len(route) - 1."""
    return [_km_between(route[i], route[i + 1]) for i in range(len(route) - 1)]


def _cumulative_km(segments: list[float]) -> list[float]:
    """누적 거리. 길이 = len(segments) + 1, 첫 값 0."""
    cum = [0.0]
    for s in segments:
        cum.append(cum[-1] + s)
    return cum


def _position_at_km(
    route: list[Position], cum_km: list[float], km_along: float
) -> Position:
    """누적 거리 km 에 해당하는 좌표를 great-circle 구간 보간으로 반환."""
    if km_along <= 0.0:
        return cast("Position", dict(route[0]))
    total = cum_km[-1]
    if km_along >= total:
        return cast("Position", dict(route[-1]))
    # 이진 검색 대신 선형 (경로 점 수 ~50, 충분히 빠름)
    for i in range(len(cum_km) - 1):
        if cum_km[i] <= km_along <= cum_km[i + 1]:
            seg_len = cum_km[i + 1] - cum_km[i]
            if seg_len <= 0:
                return cast("Position", dict(route[i]))
            frac = (km_along - cum_km[i]) / seg_len
            # great-circle 구간 보간
            brng = bearing(route[i], route[i + 1])
            return offset_position(route[i], brng, frac * seg_len)
    return cast("Position", dict(route[-1]))


def _forward_route_from(
    route: list[Position],
    cum_km: list[float],
    km_along: float,
    horizon_km: float,
) -> list[Position]:
    """현재 위치부터 horizon_km 앞까지의 점 리스트(dispatch_tick 입력).

    현재 위치를 첫 점으로 삽입하고, 이후 경로 waypoint 들을
    horizon_km 를 포함할 때까지 추가한다.
    """
    current = _position_at_km(route, cum_km, km_along)
    pts: list[Position] = [current]
    target_km = km_along + horizon_km
    for i in range(len(cum_km)):
        if cum_km[i] <= km_along:
            continue
        pts.append(cast("Position", dict(route[i])))
        if cum_km[i] >= target_km:
            break
    return pts


def _find_active_escort(
    fleet: list[Icebreaker], ship_id: str
) -> Icebreaker | None:
    for ib in fleet:
        if ib.get("escorting_ship_id") == ship_id and ib["status"] == "escorting":
            return ib
    return None


def _pick_home_port(ib_id: str) -> str:
    for flt in FLEET_BY_ROUTE.values():
        for ib in flt:
            if ib["id"] == ib_id:
                return ib["home_port"]
    return "?"


def _name_of(fleet: list[Icebreaker], ib_id: str) -> str:
    for ib in fleet:
        if ib["id"] == ib_id:
            return ib["name_ko"]
    return ib_id


# ─── 콘솔 타임라인 ────────────────────────────────────────────────────────


def _print_header(route_name: str, ship_id: str, ice_class: str,
                  speed: float, month: int, dt_hours: float,
                  start_pos: Position, start_rio: float) -> None:
    print(f"[START] route={route_name} ship={ship_id} class={ice_class} "
          f"speed={speed}kn month={month} dt={dt_hours}h")
    print(f"[t=  0h] start at ({start_pos['lat']:.2f}, {start_pos['lon']:.2f}), "
          f"RIO={start_rio:+.2f}")


def _print_events(
    t_hours: float,
    events: list[DispatchEvent],
    fleet_before: list[Icebreaker],
    fleet_after: list[Icebreaker],
    ship_pos: Position,
    actual_thick: float,
    rio_now: float,
) -> None:
    """이벤트가 있을 때만 한 줄씩 출력. 거리/시간 수치 포함."""
    for ev in events:
        etype = ev["type"]
        ib_id = ev["icebreaker_id"]
        name = _name_of(fleet_after, ib_id)

        if etype == "call":
            ib_before = next(ib for ib in fleet_before if ib["id"] == ib_id)
            dist_km = _km_between(ib_before["position"], ship_pos)
            eta_h = dist_km / (ib_before["speed_knots"] * NM_TO_KM)
            print(f"[CALL]    t={t_hours:5.1f}h {name} dispatched "
                  f"(distance {dist_km:.1f}km, ETA {eta_h:.1f}h) "
                  f"[RIO={rio_now:+.2f}]")
        elif etype == "rendezvous":
            print(f"[RDV]     t={t_hours:5.1f}h {name} rendezvous with ship")
        elif etype == "start_escort":
            ib_after = next(ib for ib in fleet_after if ib["id"] == ib_id)
            eff = effective_ice_thickness(actual_thick, ib_after)
            print(f"[ESCORT]  t={t_hours:5.1f}h {name}, effective_thickness "
                  f"{eff:.2f}m (was {actual_thick:.2f}m)")
        elif etype == "release":
            print(f"[RELEASE] t={t_hours:5.1f}h {name} released, "
                  f"returning to {_pick_home_port(ib_id)} "
                  f"[RIO={rio_now:+.2f}]")
        elif etype == "return":
            print(f"[RETURN]  t={t_hours:5.1f}h {name} back at "
                  f"{_pick_home_port(ib_id)}")
        elif etype == "intercept_failed":
            ib_before = next(
                (ib for ib in fleet_before if ib["id"] == ib_id), None
            )
            dist_km = (
                _km_between(ib_before["position"], ship_pos) if ib_before else -1.0
            )
            print(f"[INTERCEPT_FAILED] t={t_hours:5.1f}h {name} could not close "
                  f"(distance {dist_km:.1f}km, not closing >=2%/tick for "
                  f"{5} ticks), released -> relay")


# ─── 메인 시뮬 ────────────────────────────────────────────────────────────


def _apply_land_avoidance(
    route: list[Position], verbose: bool
) -> tuple[list[Position], list[Position]]:
    """정적 경로를 전역 육지 마스크로 정합(섬·반도 관통 제거).

    Returns (refined, display):
      - refined : 본선 진행용 고밀도 정합 경로(육지 비교차)
      - display : 육지 안전성을 유지한 채 정점을 최소화한 표시용 경로
    마스크 자산이 없으면(CI/배포 환경) 경고 후 원본 경로를 그대로 사용.
    """
    try:
        from pipeline.icebreaker.land_mask import (
            LandMask,
            refine_route,
            simplify_route,
        )
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[LAND]    육지 마스크 모듈 로드 불가 — 원본 경로 사용 ({e})")
        return route, route
    try:
        mask = LandMask.load()
    except Exception as e:  # noqa: BLE001 — 마스크 파일 없음 등
        if verbose:
            print(f"[LAND]    육지 마스크 자산 없음 — 원본 경로 사용 ({e})")
        return route, route
    refined = refine_route(route, mask)
    display = simplify_route(refined, mask)
    if verbose:
        print(f"[LAND]    경로 정합: {len(route)} -> {len(refined)} waypoints "
              f"(육지 회피), 표시용 {len(display)} waypoints")
    return refined, display


def simulate_voyage(
    route_name: str,
    ship_id: str,
    ship_ice_class: str,
    ship_speed_knots: float,
    month: int,
    dt_hours: float = 1.0,
    output_path: str | Path | None = None,
    verbose: bool = True,
    max_ticks: int = 5000,
    avoid_land: bool = True,
    rl_avoid: bool = False,
) -> dict[str, Any]:
    """주어진 경로/월/본선 속성으로 항해 시뮬레이션 실행.

    avoid_land=True 면 전역 육지 마스크로 경로를 정합해 섬·반도 관통을 제거.
    rl_avoid=True 면 학습된 RL 모델로 빙산 회피 디투어를 route 에 사전 베이크해,
      Voyage 재생(선미추적) 화면에서 본선이 빙산을 휘어 피하는 모습이 재현된다.
      (RL 의존성/모델 미존재 시 자동으로 회피 없이 진행 — CI 안전.)
    Returns the trace dict. Writes JSON if output_path given.
    """
    routes = load_routes()
    if route_name not in routes:
        raise ValueError(f"Unknown route: {route_name}")
    route = routes[route_name]
    display_route = route  # 표시용(육지 안전 단순화) — 기본은 원본
    if avoid_land:
        route, display_route = _apply_land_avoidance(route, verbose)

    field = IceField.from_month(month)
    pc_class = arc_to_pc(ship_ice_class)

    # RL 빙산 회피 사전 베이크 — 반드시 육지 정합 이후(detour 가 육지 정합 좌표계
    # 위에서 만들어지도록). 본선 진행은 route + km_along 스칼라에만 의존하므로
    # route 를 detour 로 교체하면 이후 tick·dispatch·escort 가 새 경로 위에서 자동 동작.
    rl_meta: dict[str, Any] = {"applied": False, "method": None, "segments": []}
    if rl_avoid:
        route, display_route, rl_meta = apply_rl_iceberg_avoidance(
            route, display_route, field, route_name,
            ice_class_pc=pc_class, ship_speed_knots=ship_speed_knots,
            verbose=verbose,
        )

    # RL 회피 윈도우(최종 route km 좌표) — tick 루프에서 진입/이탈 시 이벤트 발화.
    rl_windows = [
        {**seg, "_started": False, "_ended": False}
        for seg in rl_meta.get("segments", [])
    ]

    def rio_at_point(pos: Position) -> float:
        return calculate_rio(pc_class, field.sample(pos, month))

    segments = _segment_distances_km(route)
    cum_km = _cumulative_km(segments)
    route_total_km = cum_km[-1]

    fleet: list[Icebreaker] = [dict(ib) for ib in fleet_for_route(route_name)]  # type: ignore[misc]
    for ib in fleet:
        ib["position"] = cast("Position", dict(ib["position"]))

    km_along = 0.0
    ship_pos = _position_at_km(route, cum_km, km_along)
    start_conditions = field.sample(ship_pos, month)
    start_rio = calculate_rio(pc_class, start_conditions)
    if verbose:
        _print_header(route_name, ship_id, ship_ice_class, ship_speed_knots,
                      month, dt_hours, ship_pos, start_rio)

    ticks_out: list[dict[str, Any]] = []
    last_hour_printed = 0.0
    total_escort_km = 0.0
    max_rio_violation = 0.0
    calls = 0
    intercept_failed_count = 0
    intercept_history: dict[str, list[float]] = {}

    for tick in range(max_ticks):
        t_hours = tick * dt_hours
        ship_pos = _position_at_km(route, cum_km, km_along)
        conditions = field.sample(ship_pos, month)
        rio_now = calculate_rio(pc_class, conditions)
        # 실측 두께(m) 직접 사용 — 종전 type-table 가중 근사는 농도가 낮으면
        # 두꺼운 빙역도 0.0x m 로 과소표시되는 문제가 있었다.
        actual_thick = field.sample_thickness(ship_pos)

        escort_before = _find_active_escort(fleet, ship_id)
        eff_thick = effective_ice_thickness(actual_thick, escort_before)

        # horizon: dynamic_lookahead 가 최대 LOOKAHEAD_MAX_KM 까지 스캔할 수 있으므로
        # forward_route 는 그 이상 길이를 제공해야 함
        horizon = LOOKAHEAD_MAX_KM + 20.0
        forward_pts = _forward_route_from(route, cum_km, km_along, horizon)

        fleet_before = copy.deepcopy(fleet)
        fleet, events, intercept_history = dispatch_tick(
            ship_id=ship_id,
            ship_position=ship_pos,
            ship_ice_class=ship_ice_class,
            ship_speed_knots=ship_speed_knots,
            forward_route=forward_pts,
            rio_at_point=rio_at_point,
            icebreakers=fleet,
            dt_hours=dt_hours,
            intercept_history=intercept_history,
        )

        for ev in events:
            if ev["type"] == "call":
                calls += 1
            elif ev["type"] == "intercept_failed":
                intercept_failed_count += 1

        if rio_now < max_rio_violation:
            max_rio_violation = rio_now

        if verbose:
            _print_events(t_hours, events, fleet_before, fleet, ship_pos,
                          actual_thick, rio_now)
            # 24시간마다 상태 한 줄
            if t_hours - last_hour_printed >= 24.0 - 1e-9:
                print(f"[t={t_hours:5.1f}h] ship ({ship_pos['lat']:.2f}, "
                      f"{ship_pos['lon']:.2f}) RIO={rio_now:+.2f} "
                      f"thickness {eff_thick:.2f}m km_along={km_along:.0f}/"
                      f"{route_total_km:.0f}")
                last_hour_printed = t_hours

        # RL 회피 윈도우 진입/이탈 이벤트 — km_along 이 세그먼트 경계를 넘을 때 1회.
        # (프론트는 metadata.rl_avoidance.segments 로 배지를 구동하므로 이벤트는
        #  토스트/로그용. icebreaker_id 는 토스트 fmtEvent 호환 위해 None 명시.)
        tick_events: list[dict[str, Any]] = [dict(ev) for ev in events]
        for win in rl_windows:
            if not win["_started"] and km_along >= win["start_km"]:
                win["_started"] = True
                tick_events.append({
                    "type": "rl_avoid_start",
                    "avoidance_type": "iceberg",
                    "method": rl_meta.get("method") or "RL",
                    "confidence": win["confidence"],
                    "berg_count": win["berg_count"],
                    "icebreaker_id": None,
                })
            if win["_started"] and not win["_ended"] and km_along >= win["end_km"]:
                win["_ended"] = True
                tick_events.append({
                    "type": "rl_avoid_end",
                    "avoidance_type": "iceberg",
                    "icebreaker_id": None,
                })

        ticks_out.append({
            "t": t_hours,
            "ship": {
                "position": {"lat": ship_pos["lat"], "lon": ship_pos["lon"]},
                "rio": round(rio_now, 4),
                "thickness_m": round(actual_thick, 4),
                "effective_thickness_m": round(eff_thick, 4),
                "km_along_route": round(km_along, 2),
            },
            "icebreakers": [
                {
                    "id": ib["id"],
                    "position": {"lat": ib["position"]["lat"],
                                 "lon": ib["position"]["lon"]},
                    "status": ib["status"],
                    "escorting_ship_id": ib.get("escorting_ship_id"),
                }
                for ib in fleet
            ],
            "events": tick_events,
        })

        # 본선 이동 (escort 중이면 쇄빙선 속도 제한)
        escort_after = _find_active_escort(fleet, ship_id)
        if escort_after is not None:
            effective_speed = min(ship_speed_knots, escort_after["speed_knots"])
            total_escort_km += effective_speed * NM_TO_KM * dt_hours
        else:
            effective_speed = ship_speed_knots

        step_km = effective_speed * NM_TO_KM * dt_hours
        km_along += step_km
        if km_along >= route_total_km:
            km_along = route_total_km
            t_hours = (tick + 1) * dt_hours
            if verbose:
                print(f"[ARRIVE]  t={t_hours:5.1f}h ship arrived at "
                      f"({route[-1]['lat']:.2f}, {route[-1]['lon']:.2f}), "
                      f"total {calls} call(s), {total_escort_km:.1f}km escorted")
            break

    else:
        if verbose:
            print(f"[WARN]    simulation hit max_ticks={max_ticks}, "
                  f"progress {km_along:.0f}/{route_total_km:.0f}km")

    total_ticks = len(ticks_out)
    summary = {
        "icebreaker_calls": calls,
        "intercept_failed": intercept_failed_count,
        "total_escort_distance_km": round(total_escort_km, 2),
        "max_rio_violation": round(max_rio_violation, 4),
        "completed": km_along >= route_total_km,
        "total_route_km": round(route_total_km, 2),
    }
    trace: dict[str, Any] = {
        "metadata": {
            "route": route_name,
            "ship": {
                "id": ship_id,
                "ice_class": ship_ice_class,
                "speed_knots": ship_speed_knots,
            },
            "month": month,
            "dt_hours": dt_hours,
            "total_ticks": total_ticks,
            "duration_hours": round(total_ticks * dt_hours, 2),
            # RL 빙산 회피 베이크 결과(없으면 applied=False). 프론트 Voyage 모드가
            # segments 로 회피 배지·청록 항로선을 구동(seek 견고).
            "rl_avoidance": rl_meta,
        },
        "ticks": ticks_out,
        "summary": summary,
        # 육지 회피로 정합된 본선 항로 웨이포인트(표시용 단순화본). 프론트 Voyage
        # 모드가 이 경로를 표시선으로 그려, 화면의 항로선과 재생되는 본선 경로를
        # 일치시킨다. 본선 진행은 고밀도 refined route를 따르고, 단순화본은 같은
        # 경로 위 정점만 남긴 것이라 시각적으로 동일하다.
        "route_waypoints": [
            {"lat": p["lat"], "lon": p["lon"]} for p in display_route
        ],
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, separators=(",", ":"))
        if verbose:
            size_kb = out.stat().st_size / 1024.0
            print(f"[WRITE]   trace -> {out} ({size_kb:.1f} KB)")

    return trace


# ─── 테스트 스위트 ────────────────────────────────────────────────────────


def _separator(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def run_tests() -> None:
    """시뮬레이션 검증."""

    # TEST_SIM 1: NSR 3월 Arc4 → 쇄빙선 호출 ≥ 1
    # NOTE: Arc7/Arc9 는 RIO < -10 을 요구하는데, POLARIS PC4 RIV 테이블 상
    # 최악 단일 빙종(Glacier Ice)의 RIV 도 -6 이어서 concentration × RIV 공식으로는
    # -10 에 도달 불가. 따라서 검증용 본선은 RIO < 0 임계인 Arc4(PC7) 사용.
    _separator("TEST_SIM 1 - NSR March Arc4 (expect calls >= 1)")
    trace1 = simulate_voyage(
        route_name="NSR",
        ship_id="ship-nsr-arc4",
        ship_ice_class="Arc4",
        ship_speed_knots=15.0,
        month=3,
        dt_hours=1.0,
        output_path=None,
        verbose=False,
    )
    c1 = trace1["summary"]["icebreaker_calls"]
    print(f"  NSR/3/Arc4: calls={c1}, "
          f"completed={trace1['summary']['completed']}, "
          f"ticks={trace1['metadata']['total_ticks']}, "
          f"max_rio={trace1['summary']['max_rio_violation']}")
    assert c1 >= 1, f"expected >=1 icebreaker call in NSR March Arc4, got {c1}"
    print("  [PASS] NSR March Arc4 triggers icebreaker call(s)")

    # TEST_SIM 2: SUEZ 7월 → 쇄빙선 호출 == 0
    _separator("TEST_SIM 2 - SUEZ July (expect calls == 0)")
    trace2 = simulate_voyage(
        route_name="SUEZ",
        ship_id="ship-suez-jul",
        ship_ice_class="Arc7",
        ship_speed_knots=15.0,
        month=7,
        dt_hours=2.0,
        output_path=None,
        verbose=False,
    )
    c2 = trace2["summary"]["icebreaker_calls"]
    print(f"  SUEZ/7: calls={c2}, completed={trace2['summary']['completed']}, "
          f"ticks={trace2['metadata']['total_ticks']}")
    assert c2 == 0, f"expected 0 icebreaker calls in SUEZ July, got {c2}"
    print("  [PASS] SUEZ July has no icebreaker calls")

    # TEST_SIM 3: dt_hours 일관성 — dt=0.5 vs dt=2.0
    _separator("TEST_SIM 3 - dt_hours consistency (0.5h vs 2.0h)")
    trace_dt05 = simulate_voyage(
        "NSR", "ship-dt05", "Arc4", 15.0, month=3,
        dt_hours=0.5, output_path=None, verbose=False,
    )
    trace_dt20 = simulate_voyage(
        "NSR", "ship-dt20", "Arc4", 15.0, month=3,
        dt_hours=2.0, output_path=None, verbose=False,
    )
    dur05 = trace_dt05["metadata"]["duration_hours"]
    dur20 = trace_dt20["metadata"]["duration_hours"]
    # intercept_failed 반복 주기는 dt 에 민감하므로 call 수 대신 성공 에스코트 수로 판정.
    # 성공 에스코트(start_escort 이벤트)는 본선-쇄빙선 실제 합류를 뜻하며 dt 불변이어야 함.
    def count_start_escort(tr):
        return sum(
            1
            for tk in tr["ticks"]
            for ev in tk["events"]
            if ev["type"] == "start_escort"
        )
    esc05 = count_start_escort(trace_dt05)
    esc20 = count_start_escort(trace_dt20)
    calls05 = trace_dt05["summary"]["icebreaker_calls"]
    calls20 = trace_dt20["summary"]["icebreaker_calls"]
    rel = abs(dur05 - dur20) / max(dur05, dur20)
    print(f"  dt=0.5: {dur05}h ({calls05} calls, {esc05} successful escorts)")
    print(f"  dt=2.0: {dur20}h ({calls20} calls, {esc20} successful escorts)")
    print(f"  duration rel diff = {rel*100:.2f}%")
    assert esc05 == esc20, \
        f"successful escort count mismatch: dt0.5={esc05} vs dt2.0={esc20}"
    assert rel <= 0.05, \
        f"duration diff {rel*100:.2f}% exceeds 5% tolerance"
    print("  [PASS] dt consistency (successful escorts identical, duration within 5%)")

    # TEST_SIM 4: 릴레이 메커니즘 검증 + 클래스 간 RIO 한계 차별화
    # NOTE: calls 수는 ice_type_mapper 의 NSR 커버리지가 Glacier Ice 셀에 집중되어
    # 세 클래스 모두 동일 지점에서 트리거되므로 동일할 수 있음. threshold 차이는
    # max_rio_violation 의 절대값으로 검증한다.
    _separator("TEST_SIM 4 - intercept_failed relay + RIO differentiation")
    tr_arc4 = simulate_voyage(
        "NSR", "ship-arc4", "Arc4", 15.0, month=3,
        dt_hours=1.0, output_path=None, verbose=False,
    )
    tr_arc7 = simulate_voyage(
        "NSR", "ship-arc7", "Arc7", 15.0, month=3,
        dt_hours=1.0, output_path=None, verbose=False,
    )
    tr_arc9 = simulate_voyage(
        "NSR", "ship-arc9", "Arc9", 15.0, month=3,
        dt_hours=1.0, output_path=None, verbose=False,
    )
    for cls, tr in [("Arc4", tr_arc4), ("Arc7", tr_arc7), ("Arc9", tr_arc9)]:
        s = tr["summary"]
        print(f"  NSR/3/{cls}: calls={s['icebreaker_calls']}, "
              f"intercept_failed={s['intercept_failed']}, "
              f"escort_km={s['total_escort_distance_km']:.1f}, "
              f"max_rio={s['max_rio_violation']}")

    # 검증 1: 릴레이 메커니즘이 실제로 적어도 한 클래스에서 트리거됨
    total_failed = sum(
        tr["summary"]["intercept_failed"] for tr in (tr_arc4, tr_arc7, tr_arc9)
    )
    assert total_failed >= 1, \
        f"relay mechanism never triggered in any class (got {total_failed} intercept_failed)"

    # 검증 2: RIO 임계값 정책이 실제 max_rio 에 반영됨
    # Arc4(PC7) 는 Arc7(PC4), Arc9(PC3) 보다 더 깊은 음수 RIO 를 허용해야 함
    rio_arc4 = tr_arc4["summary"]["max_rio_violation"]
    rio_arc7 = tr_arc7["summary"]["max_rio_violation"]
    rio_arc9 = tr_arc9["summary"]["max_rio_violation"]
    assert rio_arc4 < rio_arc7 < rio_arc9 <= 0.0, \
        f"RIO order broken: Arc4={rio_arc4}, Arc7={rio_arc7}, Arc9={rio_arc9}"
    print(f"  max_rio order: Arc4({rio_arc4}) < Arc7({rio_arc7}) < Arc9({rio_arc9})")
    print("  [PASS] relay mechanism triggered + max_rio ordering Arc4<Arc7<Arc9")

    print()
    print("=" * 72)
    print("  ALL SIMULATE_VOYAGE TESTS PASSED")
    print("=" * 72)


# ─── CLI ────────────────────────────────────────────────────────────────


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="run test suite")
    parser.add_argument("--route", default="NSR")
    parser.add_argument("--ship-id", default="ship-demo")
    parser.add_argument("--ice-class", default="Arc7")
    parser.add_argument("--speed", type=float, default=15.0)
    parser.add_argument("--month", type=int, default=3)
    parser.add_argument("--dt", type=float, default=1.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.test:
        run_tests()
        return 0

    out = args.output
    if out is None:
        fname = (f"{args.route.lower()}_month{args.month:02d}_"
                 f"{args.ice_class.lower()}.json")
        out = str(DEFAULT_OUTPUT_DIR / fname)

    simulate_voyage(
        route_name=args.route,
        ship_id=args.ship_id,
        ship_ice_class=args.ice_class,
        ship_speed_knots=args.speed,
        month=args.month,
        dt_hours=args.dt,
        output_path=out,
        verbose=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
