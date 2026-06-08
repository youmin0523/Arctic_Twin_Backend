"""시뮬레이터 재실행 골든 테스트 — simulate_voyage 를 실제 재실행해
   (1) 결정성(같은 입력 → 같은 출력)과 (2) 저장된 baseline 과의 근접성을 검증한다.

baseline(data/simulations/*.json)은 과거 버전으로 생성돼 현재 코드와 미세하게
다를 수 있으므로, 거리/틱은 허용오차로 비교한다(대규모 드리프트만 실패).

실행에는 항로·해빙 데이터가 필요하므로, 데이터/의존성이 없으면 skip 한다(CI 안전).
"""
import json
from pathlib import Path
import pytest

_DATA = Path(__file__).resolve().parents[1] / "data" / "simulations"


def _load_sim():
    try:
        from pipeline.icebreaker.simulate_voyage import simulate_voyage
        return simulate_voyage
    except Exception as e:  # 의존성/경로 문제 시 skip
        pytest.skip(f"simulate_voyage import 불가: {e}")


def _run(month=1, ice_class="Arc4", route="NSR", speed=15.0, rl_avoid=False):
    sim = _load_sim()
    try:
        return sim(
            route_name=route, ship_id=f"test-{route}-{ice_class}-m{month}",
            ship_ice_class=ice_class, ship_speed_knots=speed, month=month,
            dt_hours=1.0, output_path=None, verbose=False, rl_avoid=rl_avoid,
        )
    except FileNotFoundError as e:
        pytest.skip(f"시뮬레이션 입력 데이터 없음: {e}")


def test_simulation_is_deterministic():
    """같은 입력으로 두 번 실행하면 핵심 출력이 완전히 동일해야 한다(재현성)."""
    a = _run()
    b = _run()
    assert a["metadata"]["total_ticks"] == b["metadata"]["total_ticks"]
    assert a["summary"]["total_route_km"] == pytest.approx(b["summary"]["total_route_km"])
    assert a["ticks"][-1]["ship"]["position"] == b["ticks"][-1]["ship"]["position"]
    assert a["summary"]["icebreaker_calls"] == b["summary"]["icebreaker_calls"]


def test_output_is_self_consistent():
    """재실행 결과 자체의 물리 불변식 (총거리>0, 틱=메타, 완료 시 도달)."""
    t = _run()
    assert t["summary"]["total_route_km"] > 0
    assert t["metadata"]["total_ticks"] == len(t["ticks"])
    km = [tk["ship"]["km_along_route"] for tk in t["ticks"]]
    assert km == sorted(km), "진행거리가 비감소여야 함"
    if t["summary"]["completed"]:
        assert km[-1] >= t["summary"]["total_route_km"] * 0.95


def test_close_to_baseline_within_tolerance():
    """재실행 결과가 저장된 baseline 과 허용오차 내인지 (대규모 드리프트 감지)."""
    baseline_file = _DATA / "nsr_month01_arc4.json"
    if not baseline_file.exists():
        pytest.skip("baseline 파일 없음")
    base = json.loads(baseline_file.read_text(encoding="utf-8"))
    # baseline 이 RL 회피 베이크본이면 동일 설정으로 재실행해 비교한다.
    # RL 의존성(torch/sb3·가중치)이 없어 재현 불가하면 비교를 skip(CI 안전).
    base_rl = bool(base.get("metadata", {}).get("rl_avoidance", {}).get("applied"))
    t = _run(month=1, ice_class="Arc4", route="NSR", rl_avoid=base_rl)
    if base_rl and not t.get("metadata", {}).get("rl_avoidance", {}).get("applied"):
        pytest.skip("RL 의존성 없음 — RL 베이크 baseline 재현 불가")

    # 총거리 1% 이내
    assert t["summary"]["total_route_km"] == pytest.approx(
        base["summary"]["total_route_km"], rel=0.01
    )
    # 틱 수 ±3 이내
    assert abs(t["metadata"]["total_ticks"] - base["metadata"]["total_ticks"]) <= 3
    # 완료 여부 동일
    assert t["summary"]["completed"] == base["summary"]["completed"]
