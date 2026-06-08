"""voyage trace 재생성 — 북극 3개 항로 × 12개월 × Arc4/Arc7/Arc9 = 108개.

backend/data/simulations 와 frontend/public/simulations 양쪽에 동일 출력.
naming: {route}_month{mm}_{cls}.json, ship_id=ship-{route}-{Cls}-m{mm}
  NSR → 아라온(ib-araon)      @ Wrangel 연안
  NWP → CCGS(ib-ccgs)        @ Resolute Passage
  TSR → 원자력(ib-rosatom)    @ Longyearbyen
각 항로의 함대/모항은 models.py FLEET_BY_ROUTE 가 route_name 으로 자동 선택.

특정 항로만 재생성하려면 인자 전달: python regenerate_voyage_traces.py NWP TSR
--rl-avoid 플래그를 주면 학습된 RL 모델로 빙산 회피 디투어를 trace 에 사전 베이크해
Voyage 재생(선미추적) 화면에서 RL 회피가 보이게 한다(RL 의존성/모델 필요).
  예: python regenerate_voyage_traces.py --rl-avoid
      python regenerate_voyage_traces.py --rl-avoid TSR
"""
import shutil
import sys
from pathlib import Path

# tools/ -> backend/ -> Digital_twin/
_REPO = Path(__file__).resolve().parents[2]
# tools/ 하위에서 직접 실행 시에도 backend/ 의 pipeline 패키지를 import 가능하게
sys.path.insert(0, str(_REPO / "backend"))

from pipeline.icebreaker.simulate_voyage import simulate_voyage  # noqa: E402
BACKEND_OUT = _REPO / "backend" / "data" / "simulations"
FRONTEND_OUT = _REPO / "frontend" / "public" / "simulations"
CLASSES = ["Arc4", "Arc7", "Arc9"]
ALL_ROUTES = ["NSR", "NWP", "TSR"]

# CLI 인자: --rl-avoid 플래그 + 항로 부분 재생성(예: ... NWP TSR). 미지정 시 전체.
_raw = sys.argv[1:]
RL_AVOID = any(a.lower() in ("--rl-avoid", "--rl_avoid", "--rl") for a in _raw)
_args = [a.upper() for a in _raw if not a.startswith("-")]
ROUTES = [r for r in ALL_ROUTES if r in _args] or ALL_ROUTES

BACKEND_OUT.mkdir(parents=True, exist_ok=True)
FRONTEND_OUT.mkdir(parents=True, exist_ok=True)

summary_rows = []
for route in ROUTES:
    rlow = route.lower()
    for month in range(1, 13):
        for cls in CLASSES:
            mm = f"{month:02d}"
            fname = f"{rlow}_month{mm}_{cls.lower()}.json"
            out = BACKEND_OUT / fname
            trace = simulate_voyage(
                route_name=route,
                ship_id=f"ship-{rlow}-{cls}-m{mm}",
                ship_ice_class=cls,
                ship_speed_knots=15.0,
                month=month,
                dt_hours=1.0,
                output_path=out,
                verbose=False,
                rl_avoid=RL_AVOID,
            )
            shutil.copyfile(out, FRONTEND_OUT / fname)
            s = trace["summary"]
            rla = trace["metadata"].get("rl_avoidance", {})
            summary_rows.append(
                (fname, s["icebreaker_calls"], s["intercept_failed"],
                 round(s["total_escort_distance_km"], 0), s["max_rio_violation"],
                 len(rla.get("segments", [])))
            )

print(f"{'file':28s} {'calls':>5s} {'fail':>4s} {'esc_km':>8s} {'max_rio':>8s} {'rl_seg':>6s}")
for r in summary_rows:
    print(f"{r[0]:28s} {r[1]:5d} {r[2]:4d} {r[3]:8.0f} {r[4]:8.2f} {r[5]:6d}")

n = len(summary_rows)
total_with_calls = sum(1 for r in summary_rows if r[1] > 0)
total_with_escort = sum(1 for r in summary_rows if r[3] > 0)
total_with_rl = sum(1 for r in summary_rows if r[5] > 0)
print(f"\nroutes={ROUTES} files={n} rl_avoid={RL_AVOID}")
print(f"files with calls>0: {total_with_calls}/{n}, "
      f"files with successful escort_km>0: {total_with_escort}/{n}, "
      f"files with RL avoidance segments>0: {total_with_rl}/{n}")
