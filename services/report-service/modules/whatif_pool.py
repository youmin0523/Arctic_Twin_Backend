"""
whatif_pool.py
==============
하드코딩 시나리오 풀의 import 경로 단순화 alias 모듈.

본체는 whatif_generator.HARDCODED_SCENARIO_POOL이며, 이 모듈은 re-export만 담당합니다.
다른 모듈(예: whatif_generator_max)에서 `from .whatif_pool import HARDCODED_SCENARIO_POOL`
형태로 가져올 수 있도록 alias를 제공합니다.

기존 generator의 풀 정의를 옮기지 않으므로 호환성에 영향 없음.
"""

from .whatif_generator import (
    HARDCODED_SCENARIO_POOL,
    HARDCODED_MIN,
    HARDCODED_MAX,
)

__all__ = ["HARDCODED_SCENARIO_POOL", "HARDCODED_MIN", "HARDCODED_MAX"]
