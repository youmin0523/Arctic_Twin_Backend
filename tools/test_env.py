import sys
import os

print("--- 환경 테스트 시작 ---")
print(f"Python: {sys.version}")
print(f"CWD: {os.getcwd()}")

try:
    import numpy
    print(f"Numpy loaded: {numpy.__version__}")
    
    import gymnasium
    print(f"Gymnasium loaded: {gymnasium.__version__}")
    
    # 육지 마스크 테스트
    sys.path.append(os.path.join(os.getcwd(), 'backend', 'services', 'rl-pipeline'))
    from modules.rl_land_mask import LandMask
    lm = LandMask()
    print("LandMask initialized successfully")
    
    test_lat, test_lon = 70.0, -40.0 # 그린란드 추정
    is_land = lm.is_land(test_lat, test_lon)
    print(f"Test location ({test_lat}, {test_lon}) -> Land: {is_land}")

    print("--- 테스트 성공 ---")
except Exception as e:
    print(f"--- 테스트 실패 ---")
    print(e)
    import traceback
    traceback.print_exc()
