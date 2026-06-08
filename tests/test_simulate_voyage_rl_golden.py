"""RL 빙산 회피 사전 베이크(rl_avoid=True) 골든 테스트.

검증:
  (1) 결정성 — 같은 입력으로 두 번 베이크하면 trace 가 완전히 동일.
  (2) 효과 — rl_avoid=True 가 rl_avoid=False 대비 route 를 바꾸고, rl_avoid_start
      이벤트 + metadata.rl_avoidance.segments 를 생성하며 본선 경로가 실제로 이탈.
  (3) 불변식 — 회피 후에도 km_along 비감소·좌표 유효.

RL 의존성(torch/stable-baselines3·학습 가중치)이 없거나 해당 월/항로에 위협이
없어 회피가 적용되지 않으면 skip 한다(CI 안전).
"""
import json
import hashlib

import pytest


def _load_sim():
    try:
        from pipeline.icebreaker.simulate_voyage import simulate_voyage
        return simulate_voyage
    except Exception as e:  # 의존성/경로 문제 시 skip
        pytest.skip(f"simulate_voyage import 불가: {e}")


def _run(rl_avoid, month=1, ice_class="Arc4", route="NSR"):
    sim = _load_sim()
    try:
        return sim(
            route_name=route, ship_id=f"rltest-{route}-{ice_class}-m{month}",
            ship_ice_class=ice_class, ship_speed_knots=15.0, month=month,
            dt_hours=1.0, output_path=None, verbose=False, rl_avoid=rl_avoid,
        )
    except FileNotFoundError as e:
        pytest.skip(f"시뮬레이션 입력 데이터 없음: {e}")


def _require_rl_applied(trace):
    """RL 회피가 실제 적용된 trace 가 아니면 skip(RL 미설치/위협 없음)."""
    if not trace.get("metadata", {}).get("rl_avoidance", {}).get("applied"):
        pytest.skip("RL 회피 미적용 — RL 의존성 없음 또는 위협 없음")


def _digest(trace):
    return hashlib.md5(
        json.dumps(trace, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def test_rl_bake_is_deterministic():
    """같은 입력으로 RL 베이크 두 번 → trace 완전 동일(재현성)."""
    a = _run(True)
    _require_rl_applied(a)
    b = _run(True)
    assert _digest(a) == _digest(b), "RL 베이크가 비결정적"


def test_rl_bake_changes_route_and_emits_events():
    """rl_avoid=True 가 경로를 바꾸고 회피 이벤트/세그먼트를 만든다."""
    rl = _run(True)
    _require_rl_applied(rl)
    base = _run(False)

    segs = rl["metadata"]["rl_avoidance"]["segments"]
    assert len(segs) >= 1, "회피 세그먼트가 1개 이상이어야 함"
    for s in segs:
        assert s["end_km"] >= s["start_km"]
        assert 0.0 <= s["confidence"] <= 1.0

    starts = [
        ev for tk in rl["ticks"] for ev in tk["events"]
        if ev["type"] == "rl_avoid_start"
    ]
    assert len(starts) == len(segs), "rl_avoid_start 이벤트 수 = 세그먼트 수"
    for ev in starts:
        assert ev["avoidance_type"] == "iceberg"
        assert "icebreaker_id" in ev  # 프론트 fmtEvent 호환

    # route_waypoints 가 디투어로 달라져야 함(베이크 OFF 대비)
    assert rl["route_waypoints"] != base["route_waypoints"], "RL 베이크가 경로를 바꾸지 않음"


def test_rl_bake_ship_deviates_from_baseline():
    """회피 구간에서 본선 좌표가 베이크 OFF 경로 대비 실제 이탈(가시적 회피)."""
    rl = _run(True)
    _require_rl_applied(rl)
    base = _run(False)

    def pos_at_km(trace, km):
        best = None
        bestd = 1e18
        for tk in trace["ticks"]:
            d = abs(tk["ship"]["km_along_route"] - km)
            if d < bestd:
                bestd, best = d, tk["ship"]["position"]
        return best

    seg = rl["metadata"]["rl_avoidance"]["segments"][0]
    mid_km = (seg["start_km"] + seg["end_km"]) / 2.0
    p_rl = pos_at_km(rl, mid_km)
    p_base = pos_at_km(base, mid_km)
    # 같은 진행거리에서 위경도 차 — 회피로 최소 수백 m 이상 벌어져야 함
    dlat = abs(p_rl["lat"] - p_base["lat"])
    dlon = abs(p_rl["lon"] - p_base["lon"])
    assert (dlat + dlon) > 1e-3, "회피 구간에서 경로 이탈이 감지되지 않음"


def test_rl_bake_preserves_invariants():
    """회피 후에도 진행거리 비감소·좌표 유효."""
    rl = _run(True)
    _require_rl_applied(rl)
    km = [tk["ship"]["km_along_route"] for tk in rl["ticks"]]
    assert km == sorted(km), "회피 후 진행거리가 역행"
    for tk in rl["ticks"]:
        lat = tk["ship"]["position"]["lat"]
        lon = tk["ship"]["position"]["lon"]
        assert -90 <= lat <= 90 and -180 <= lon <= 180
    assert rl["metadata"]["total_ticks"] == len(rl["ticks"])
