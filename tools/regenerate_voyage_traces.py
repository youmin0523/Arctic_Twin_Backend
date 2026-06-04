"""36개 voyage trace(NSR × 12개월 × Arc4/Arc7/Arc9) 재생성.

backend/data/simulations 와 frontend/public/simulations 양쪽에 동일 출력.
naming: nsr_month{mm}_{cls}.json, ship_id=ship-nsr-{Cls}-m{mm}
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

BACKEND_OUT.mkdir(parents=True, exist_ok=True)
FRONTEND_OUT.mkdir(parents=True, exist_ok=True)

summary_rows = []
for month in range(1, 13):
    for cls in CLASSES:
        mm = f"{month:02d}"
        fname = f"nsr_month{mm}_{cls.lower()}.json"
        out = BACKEND_OUT / fname
        trace = simulate_voyage(
            route_name="NSR",
            ship_id=f"ship-nsr-{cls}-m{mm}",
            ship_ice_class=cls,
            ship_speed_knots=15.0,
            month=month,
            dt_hours=1.0,
            output_path=out,
            verbose=False,
        )
        shutil.copyfile(out, FRONTEND_OUT / fname)
        s = trace["summary"]
        summary_rows.append(
            (fname, s["icebreaker_calls"], s["intercept_failed"],
             round(s["total_escort_distance_km"], 0), s["max_rio_violation"])
        )

print(f"{'file':28s} {'calls':>5s} {'fail':>4s} {'esc_km':>8s} {'max_rio':>8s}")
for r in summary_rows:
    print(f"{r[0]:28s} {r[1]:5d} {r[2]:4d} {r[3]:8.0f} {r[4]:8.2f}")

total_with_calls = sum(1 for r in summary_rows if r[1] > 0)
total_with_escort = sum(1 for r in summary_rows if r[3] > 0)
print(f"\nfiles with calls>0: {total_with_calls}/36, "
      f"files with successful escort_km>0: {total_with_escort}/36")
