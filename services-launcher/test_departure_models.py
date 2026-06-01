"""
29개 출항 결정 ONNX 모델을 (ice_class, ship_type) 조합으로 하나씩
DepartureAgent로 로드하고 더미 obs로 inference 한 번씩 돌립니다.
"""
import sys
from pathlib import Path

import numpy as np

# Digital_twin/services-launcher/ 위치 기준 상대경로
REPORT_DIR = Path(__file__).resolve().parent.parent / "backend" / "services" / "report-service"
sys.path.insert(0, str(REPORT_DIR))

from modules.rl.departure_agent import DepartureAgent  # noqa: E402

ICE_CLASSES = ["IA Super", "IA", "PC3", "PC4", "PC5", "PC6", "PC7"]
SHIP_TYPES = ["bulk", "container", "lng", "tanker"]

# 28-dim 더미 observation (모델 출력만 sanity check 목적)
rng = np.random.default_rng(42)
DUMMY_OBS = rng.standard_normal(28).astype(np.float32)


def main():
    print(f"{'ice_class':<10} {'ship':<10} {'loaded':<7} {'action':<10} {'format':<6}")
    print("-" * 56)
    ok_count = 0
    fail_count = 0

    # default (ice_class=PC5, ship_type=default) 도 한 번 확인
    print("=== default (departure_sac.onnx) ===")
    a = DepartureAgent()
    md = a.get_metadata()
    if a.is_trained:
        action, _ = a.predict(DUMMY_OBS)
        assert action is not None
        print(f"{'PC5':<10} {'default':<10} OK      {float(action[0]):+8.4f}  {md['model_format']}")
        ok_count += 1
    else:
        print(f"{'PC5':<10} {'default':<10} FAIL")
        fail_count += 1

    print("\n=== per (ice_class, ship_type) ===")
    for ic in ICE_CLASSES:
        for st in SHIP_TYPES:
            try:
                a = DepartureAgent(ice_class=ic, ship_type=st)
                md = a.get_metadata()
                if not a.is_trained:
                    print(f"{ic:<10} {st:<10} FAIL     no model")
                    fail_count += 1
                    continue
                action, _ = a.predict(DUMMY_OBS)
                assert action is not None
                print(f"{ic:<10} {st:<10} OK      {float(action[0]):+8.4f}  {md['model_format']}")
                ok_count += 1
            except Exception as e:
                print(f"{ic:<10} {st:<10} ERROR    {e}")
                fail_count += 1

    print("-" * 56)
    print(f"OK: {ok_count}   FAIL: {fail_count}   TOTAL: {ok_count + fail_count}")


if __name__ == "__main__":
    main()
