"""
whatif_iterative_runner.py
===========================
What-If Analysis 자동 반복 개선 러너.

What-if는 강화학습 보상이 아닌 "시나리오 완성도"를 품질 지표로 사용:
  - scenarios_count      : 생성된 시나리오 수 (목표: ≥ 4)
  - avg_rio_spread       : 시나리오 간 avg_rio 분산 (클수록 다양한 시나리오)
  - recommendations_dist : 추천/조건부/비추천 고른 분포
  - tool_calls_efficiency : 도구 호출 대비 유효 시나리오 비율

반복 전략:
  - 시나리오 부족    → forecast_days 확대, 다른 route 추가
  - 다양성 낮음      → ice_class 확대 (PC3~PC7), 계절 변화 포함
  - 모두 "비추천"    → 덜 극단적 조건 (낮은 ice_concentration 시나리오 추가)
  - 완성도 충분      → 수렴 판정
"""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("report-service.whatif_iterative")

_BASE         = Path(__file__).resolve().parent.parent
HISTORY_PATH  = _BASE / "data" / "whatif_iterative_history.json"

# ── 수렴 기준 ──────────────────────────────────────────────
# whatif_generator가 매 호출마다 6~8개의 시나리오(Claude 핵심 3개 + 하드코딩 풀
# 무작위 3~5개)를 반환하므로, 시나리오 수 목표를 그에 맞춰 6으로 올린다.
# 또한 풀에서 PC1~PC7, 해빙 ×0.3~×2.0, 다양한 항로 조합이 추출되므로 avg_rio 분포가
# 자연스럽게 넓어진다. 단순 통과를 막기 위해 다양성 임계도 0.30으로 상향.
TARGET_SCENARIOS    = 6      # 최소 시나리오 수 (생성기 출력 하한과 정렬)
TARGET_RIO_SPREAD   = 0.30   # 시나리오 간 avg_rio max-min 폭 (의미 있는 다양성)
MIN_TOOL_EFFICIENCY = 0.30   # 도구 호출 효율 (유효 시나리오 / 전체 호출)

# 탐색할 route/ice_class 조합 풀
ROUTE_POOL      = ["NSR", "NWP", "TSR"]
ICE_CLASS_POOL  = ["PC3", "PC4", "PC5", "PC6", "PC7"]
FORECAST_POOL   = [30, 60, 90]


@dataclass
class WhatIfIterRecord:
    iteration: int
    config: dict               # route, ice_class, forecast_days, departure_date
    quality: dict              # scenarios_count, avg_rio_spread, recommendations_dist, tool_efficiency
    signals: list[str]
    improved: bool
    converged: bool
    duration_seconds: float
    result_path: str = ""      # JSON 결과 파일 경로
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class WhatIfQualityAnalyzer:
    """What-if 결과의 완성도/다양성을 분석하고 개선 방향을 제안한다."""

    def score(self, result: dict) -> dict:
        """결과 딕셔너리에서 품질 지표를 계산한다."""
        scenarios = result.get("scenarios", [])
        n = len(scenarios)

        # avg_rio 분산 (시나리오 다양성 지표)
        rios = [s.get("route_summary", {}).get("avg_rio", None) for s in scenarios]
        rios = [r for r in rios if r is not None]
        rio_spread = float(max(rios) - min(rios)) if len(rios) >= 2 else 0.0

        # 추천 분포
        recs = [s.get("recommendation", "") for s in scenarios]
        rec_dist = {
            "추천":    recs.count("추천"),
            "조건부":  recs.count("조건부"),
            "비추천":  recs.count("비추천"),
        }

        # 도구 호출 효율
        tool_calls = result.get("tool_calls_count", 1)
        efficiency = n / max(tool_calls, 1)

        # 텍스트 완성도 (비교 분석 텍스트 길이)
        comparison_len = len(result.get("comparison_text", ""))

        return {
            "scenarios_count":      n,
            "avg_rio_spread":       round(rio_spread, 4),
            "recommendations_dist": rec_dist,
            "tool_efficiency":      round(efficiency, 3),
            "comparison_text_len":  comparison_len,
        }

    def analyze_signals(self, quality: dict, prev_quality: dict | None) -> list[str]:
        signals = []
        n         = quality["scenarios_count"]
        spread    = quality["avg_rio_spread"]
        rec_dist  = quality["recommendations_dist"]
        efficiency = quality["tool_efficiency"]

        if n < TARGET_SCENARIOS:
            signals.append("insufficient_scenarios")
        if spread < TARGET_RIO_SPREAD:
            signals.append("low_diversity")
        if efficiency < MIN_TOOL_EFFICIENCY:
            signals.append("low_efficiency")

        # 추천 분포 불균형
        total_recs = sum(rec_dist.values())
        if total_recs > 0:
            비추천_pct = rec_dist.get("비추천", 0) / total_recs
            추천_pct   = rec_dist.get("추천", 0) / total_recs
            if 비추천_pct >= 0.8:
                signals.append("all_negative")     # 전부 비추천 → 극단적 조건
            if 추천_pct >= 0.8:
                signals.append("all_positive")     # 전부 추천 → 조건 너무 쉬움

        # 텍스트 완성도
        if quality["comparison_text_len"] < 200:
            signals.append("shallow_analysis")

        # 이전 대비 개선
        if prev_quality:
            prev_n = prev_quality.get("scenarios_count", 0)
            if n <= prev_n and spread <= prev_quality.get("avg_rio_spread", 0) + 0.01:
                signals.append("no_improvement")

        if not signals:
            signals.append("converging")

        return signals

    @staticmethod
    def converged(quality: dict, prev_quality: dict | None) -> bool:
        if quality["scenarios_count"] < TARGET_SCENARIOS:
            return False
        if quality["avg_rio_spread"] < TARGET_RIO_SPREAD:
            return False
        if prev_quality is None:
            return False
        # 이전보다 나빠지지 않고 목표 달성
        return True


class WhatIfConfigEvolver:
    """시그널에 따라 다음 What-if 실행 설정을 조정한다."""

    def next_config(self, current: dict, signals: list[str],
                    history: list[WhatIfIterRecord], iteration: int) -> dict:
        c = deepcopy(current)

        # 사용된 route/ice_class 추적
        used_routes  = {r.config["route"] for r in history}
        used_classes = {r.config["ice_class"] for r in history}

        for sig in signals:
            if sig == "insufficient_scenarios":
                # forecast_days 확장으로 더 넓은 시나리오 공간
                idx = FORECAST_POOL.index(c["forecast_days"]) if c["forecast_days"] in FORECAST_POOL else 0
                c["forecast_days"] = FORECAST_POOL[min(idx + 1, len(FORECAST_POOL) - 1)]

            elif sig == "low_diversity":
                # 새 ice_class 탐색
                for ic in ICE_CLASS_POOL:
                    if ic not in used_classes:
                        c["ice_class"] = ic
                        break

            elif sig == "all_negative":
                # 조건을 완화: 더 좋은 얼음 조건 클래스로
                worse_classes = ["PC3", "PC4"]
                better_classes = ["PC6", "PC7"]
                if c["ice_class"] in worse_classes:
                    c["ice_class"] = better_classes[0]

            elif sig == "all_positive":
                # 조건을 강화: 더 나쁜 얼음 클래스
                if c["ice_class"] in ["PC6", "PC7"]:
                    c["ice_class"] = "PC4"

            elif sig in ("no_improvement", "shallow_analysis"):
                # 새 route로 전환
                for rt in ROUTE_POOL:
                    if rt not in used_routes:
                        c["route"] = rt
                        break

        # 항상 departure_date를 오늘로 갱신
        c["departure_date"] = date.today().isoformat()

        return c


class WhatIfIterativeRunner:
    """What-If Analysis 자동 반복 개선 러너."""

    def __init__(self, route_scorer, data_loader, status_callback=None):
        self._route_scorer    = route_scorer
        self._data_loader     = data_loader
        self._analyzer        = WhatIfQualityAnalyzer()
        self._evolver         = WhatIfConfigEvolver()
        self._history: list[WhatIfIterRecord] = []
        self._status_callback = status_callback
        self._load_history()

    def _load_history(self):
        if HISTORY_PATH.exists():
            try:
                data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                self._history = [WhatIfIterRecord(**r) for r in data]
                logger.info("What-if 이전 히스토리 %d건 복원", len(self._history))
            except Exception as e:
                logger.warning("What-if 히스토리 로드 실패: %s", e)

    def _save_history(self):
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self._history], f, indent=2, ensure_ascii=False)

    def _update_status(self, stage: str, progress: int, **extra):
        if self._status_callback:
            self._status_callback(stage=stage, progress=progress, **extra)

    def _initial_config(self) -> dict:
        return {
            "route":          "NSR",
            "ice_class":      "PC5",
            "departure_date": date.today().isoformat(),
            "forecast_days":  30,
        }

    def _last_config(self) -> dict:
        if self._history:
            return deepcopy(self._history[-1].config)
        return self._initial_config()

    def _last_quality(self) -> dict | None:
        if self._history:
            return self._history[-1].quality
        return None

    def _run_one(self, config: dict, iteration: int) -> tuple[dict, dict, float]:
        """1회 What-if 실행 → (result_dict, quality, elapsed_sec)."""
        import time
        import sys

        # report-service 경로 추가
        report_svc = str(Path(__file__).parent.parent)
        if report_svc not in sys.path:
            sys.path.insert(0, report_svc)

        t0 = time.time()

        self._update_status(
            stage=f"What-if Iter {iteration} — Claude API로 시나리오 생성 중 ({config['route']}, {config['ice_class']})...",
            progress=int(20 + iteration * 15),
        )

        from modules.whatif_generator import WhatIfGenerator
        generator = WhatIfGenerator(self._route_scorer, self._data_loader)

        result_obj = generator.generate_scenarios(
            route=config["route"],
            ice_class=config["ice_class"],
            departure_date=config["departure_date"],
            forecast_days=config["forecast_days"],
        )

        result_dict = {
            "scenarios": [
                {
                    "name":          s.name,
                    "description":   s.description,
                    "route_summary": s.route_summary,
                    "recommendation": s.recommendation,
                }
                for s in result_obj.scenarios
            ],
            "comparison_text":   result_obj.comparison_text,
            "ai_recommendation": result_obj.ai_recommendation,
            "tool_calls_count":  result_obj.tool_calls_count,
            "route":             config["route"],
            "ice_class":         config["ice_class"],
            "generated_at":      datetime.now().isoformat(),
        }

        # 결과 파일 저장
        out_path = _BASE / "data" / f"whatif_iter{iteration}_result.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result_dict, f, indent=2, ensure_ascii=False)

        # 최신 결과도 덮어쓰기
        latest_path = _BASE / "data" / "whatif_latest_result.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(result_dict, f, indent=2, ensure_ascii=False)

        quality = self._analyzer.score(result_dict)
        return result_dict, quality, time.time() - t0

    def run(self, max_iterations: int = 3, force_restart: bool = False) -> dict:
        if force_restart:
            self._history = []

        start_iter = len(self._history)
        logger.info("What-if 자동 반복 시작 (이미 %d회, 최대 %d회)", start_iter, max_iterations)

        config = self._last_config()

        for i in range(start_iter, max_iterations):
            iteration_num = i + 1
            logger.info("[What-if Iter %d] route=%s, ice_class=%s, days=%d",
                        iteration_num, config["route"], config["ice_class"], config["forecast_days"])

            try:
                result_dict, quality, elapsed = self._run_one(config, iteration_num)
            except Exception as e:
                logger.error("[What-if Iter %d] 실패: %s", iteration_num, e, exc_info=True)
                break

            prev_quality = self._last_quality()
            signals      = self._analyzer.analyze_signals(quality, prev_quality)
            converged    = self._analyzer.converged(quality, prev_quality)
            improved     = (prev_quality is None or
                            quality["scenarios_count"] > prev_quality.get("scenarios_count", 0) or
                            quality["avg_rio_spread"] > prev_quality.get("avg_rio_spread", 0))

            record = WhatIfIterRecord(
                iteration=iteration_num,
                config=deepcopy(config),
                quality=quality,
                signals=signals,
                improved=improved,
                converged=converged,
                duration_seconds=round(elapsed, 1),
                result_path=str(_BASE / "data" / f"whatif_iter{iteration_num}_result.json"),
            )
            self._history.append(record)
            self._save_history()

            logger.info("[What-if Iter %d] 시나리오=%d, spread=%.4f | 시그널: %s | 수렴: %s",
                        iteration_num, quality["scenarios_count"],
                        quality["avg_rio_spread"], signals, converged)

            self._update_status(
                stage=(f"What-if Iter {iteration_num} 완료 — "
                       f"시나리오 {quality['scenarios_count']}개, "
                       f"RIO 다양성={quality['avg_rio_spread']:.3f}"),
                progress=int(30 + iteration_num * 20),
                quality=quality,
                signals=signals,
            )

            if converged:
                logger.info("[What-if] 완성도 달성 — 반복 종료")
                break

            if i < max_iterations - 1:
                config = self._evolver.next_config(config, signals, self._history, iteration_num)

        best = (max(self._history,
                    key=lambda r: (r.quality["scenarios_count"] +
                                   r.quality["avg_rio_spread"] * 10))
                if self._history else None)

        result = {
            "total_iterations": len(self._history),
            "converged": self._history[-1].converged if self._history else False,
            "best_quality": best.quality if best else {},
            "best_iteration": best.iteration if best else 0,
            "history": [asdict(r) for r in self._history],
            "finished_at": datetime.now().isoformat(),
        }
        return result

    def get_status(self) -> dict:
        if not self._history:
            return {"iterations_done": 0, "best_quality": {}, "history": []}
        best = max(self._history,
                   key=lambda r: r.quality["scenarios_count"] + r.quality["avg_rio_spread"] * 10)
        return {
            "iterations_done": len(self._history),
            "converged": self._history[-1].converged,
            "best_quality": best.quality,
            "best_iteration": best.iteration,
            "last_signals": self._history[-1].signals,
            "history": [asdict(r) for r in self._history],
        }
