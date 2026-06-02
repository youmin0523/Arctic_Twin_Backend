"""pytest 공통 설정 — 백엔드 Python 순수 로직 테스트용 sys.path 부트스트랩.

report-service / pipeline 모듈을 무거운 의존성(torch·DB) 없이 import 할 수 있도록
경로만 추가한다. (테스트 대상은 순수 함수들이라 DataLoader/DB 는 건드리지 않음)
"""
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
for _p in (
    _BACKEND,                                  # `import pipeline.icebreaker.simulate_voyage`
    _BACKEND / "pipeline",                      # bare `import arctic_master_router`
    _BACKEND / "services" / "report-service",   # `import modules.route_scorer`
):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
