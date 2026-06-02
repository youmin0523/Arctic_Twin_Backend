"""
9개 회피항로 모델(NSR/NWP/TSR x easy/normal/hard)을 하나씩 로드해서
간단한 inference 한 번씩 돌려보고 결과를 출력합니다.
"""
import sys
from pathlib import Path

# rl-pipeline 모듈 경로 추가 (스크립트 위치 기준 상대경로)
# Digital_twin/services-launcher/test_*.py 위치 가정
RL_DIR = Path(__file__).resolve().parent.parent / "backend" / "services" / "rl-pipeline"
sys.path.insert(0, str(RL_DIR))

from modules.rl_agent import IcebergAvoidanceAgent  # noqa: E402  # type: ignore[import]

ROUTES = ["NSR", "NWP", "TSR"]
DIFFS = ["easy", "normal", "hard"]
SAMPLE_OBS = None  # 환경에서 자동 생성


def main():
    print(f"{'Model':<14} {'Loaded':<8} {'Action':<28} {'Value':<10}")
    print("-" * 70)
    for route in ROUTES:
        for diff in DIFFS:
            key = f"{route}_{diff}"
            try:
                agent = IcebergAvoidanceAgent(model_key=key)
                ok = agent.load()
                if not ok:
                    print(f"{key:<14} FAIL     no model")
                    continue
                agent.create_env(difficulty=diff)
                obs, _ = agent.env.reset()
                action, _ = agent.predict(obs, deterministic=True)
                # value 추정 (predict의 두번째 값을 기존 코드가 쓰는지 확인)
                # 단순히 action만 표시
                action_str = f"[{action[0]:+6.2f}, {action[1]:+5.2f}]"
                print(f"{key:<14} OK       {action_str:<28}")
            except Exception as e:
                print(f"{key:<14} ERROR    {e}")


if __name__ == "__main__":
    main()
