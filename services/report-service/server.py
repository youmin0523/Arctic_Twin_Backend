"""
Arctic Trend Report Service — FastAPI :8002

Run: uvicorn server:app --reload --port 8002
(포트 8002: 기존 백엔드 8000, RL 8001과 충돌 방지)
"""

import asyncio
import logging
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import cast

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── Python 3.14 + anyio 호환성 패치 ──────────────────────────────────
# anyio.to_thread.run_sync 가 Python 3.14에서 task state weakref 에러를 발생.
# asyncio.to_thread 로 우회 (sniffio/cancel-scope 거치지 않음).
import anyio.to_thread as _ato  # noqa: E402
import starlette.background as _sb  # noqa: E402


async def _patched_anyio_to_thread_run_sync(func, *args, **kwargs):
    # cancellable / limiter / abandon_on_cancel 인자 무시 (asyncio.to_thread는 미지원)
    return await asyncio.to_thread(func, *args)


_ato.run_sync = _patched_anyio_to_thread_run_sync


async def _patched_bg_call(self):
    if self.is_async:
        await self.func(*self.args, **self.kwargs)
    else:
        await asyncio.to_thread(self.func, *self.args, **self.kwargs)


_sb.BackgroundTask.__call__ = _patched_bg_call

# backend/.env 로드 — Digital_twin/backend/.env 우선, 단독 폴더 폴백
for _env in [
    Path(__file__).resolve().parents[2] / ".env",  # backend/services/report-service → backend/.env (현재 레이아웃)
    Path(__file__).resolve().parents[2] / "backend" / ".env",
    Path(__file__).parent.parent / "backend" / ".env",  # 단독 배포(HF) 시
]:
    if _env.exists():
        load_dotenv(_env)
        break

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("report-service")

app = FastAPI(
    title="Arctic Trend Report Service",
    description="북극 항로 AI 동향 보고서 생성 API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 모듈 임포트 ──────────────────────────────────────────────
from modules.data_loader import DataLoader
from modules.route_scorer import RouteScorer
from modules.trend_analyzer import TrendAnalyzer
from modules.pdf_generator import PdfGenerator
from modules.rl.departure_agent import DepartureAgent
from modules.rl.departure_trainer import DepartureTrainer
from modules.rl.departure_iterative_trainer import DepartureIterativeTrainer
from modules.rl.multi_model_trainer import MultiModelIterativeTrainer, ALL_COMBINATIONS, SHIP_TYPES
from modules.rl.prediction_calibrator import PredictionCalibrator
from modules.rl import existing_rl_client
from modules.whatif_generator_openai import WhatIfGeneratorOpenAI

# ── 싱글톤 초기화 ────────────────────────────────────────────
data_loader = DataLoader()
route_scorer = RouteScorer(data_loader)
trend_analyzer = TrendAnalyzer()
pdf_generator = PdfGenerator()
departure_agent = DepartureAgent()
departure_trainer = DepartureTrainer()
departure_iterative_trainer = DepartureIterativeTrainer(departure_trainer=departure_trainer)
multi_model_trainer = MultiModelIterativeTrainer()
calibrator = PredictionCalibrator()
whatif_generator = WhatIfGeneratorOpenAI(route_scorer, data_loader)  # v4: OpenAI gpt-4o-mini + RIO + 6~8개 보장

OUTPUT_DIR = Path(__file__).parent / "output"

# ── 인메모리 Job 관리 ────────────────────────────────────────
jobs: dict[str, dict] = {}


def _create_job() -> str:
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "progress": 0, "pdf_path": None, "error": None}
    return job_id


def _update_job(job_id: str, progress: int, status: str = "running"):
    if job_id in jobs:
        jobs[job_id]["progress"] = progress
        jobs[job_id]["status"] = status


def _complete_job(job_id: str, pdf_path: Path):
    if job_id in jobs:
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["pdf_path"] = str(pdf_path)


def _fail_job(job_id: str, error: str):
    if job_id in jobs:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = error


# ── What-If 실행 통계 누적 ────────────────────────────────────
# 완료된 What-If 실행을 backend/data/whatif_stats.json 에 적산한다.
# (backend/data 는 docker named volume 이라 컨테이너 재시작 후에도 유지)
WHATIF_STATS_PATH = Path(__file__).resolve().parents[2] / "data" / "whatif_stats.json"


def _load_whatif_runs() -> list:
    import json
    try:
        if WHATIF_STATS_PATH.exists():
            return json.loads(WHATIF_STATS_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("whatif stats 로드 실패: %s", e)
    return []


def _save_whatif_run(record: dict):
    import json
    try:
        runs = _load_whatif_runs()
        runs.append(record)
        runs = runs[-500:]  # 최근 500건만 유지 (파일 비대화 방지)
        WHATIF_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WHATIF_STATS_PATH.write_text(
            json.dumps(runs, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("whatif stats 저장 실패: %s", e)


def _convergence_status(scenarios: list) -> str:
    """프론트(WhatIfPanel)와 동일 규칙으로 수렴 상태를 분류."""
    def _is_hyp(s: dict) -> bool:
        if s.get("is_hypothetical") is True:
            return True
        t = (s.get("name") or s.get("label") or "")
        return "[HYP]" in t or "【가설】" in t

    real = [s for s in scenarios if not _is_hyp(s)]
    n = len(real)
    if n == 0:
        return "stalled"
    rec = {"추천": 0, "조건부": 0, "비추천": 0}
    for s in real:
        r = s.get("recommendation")
        if r in rec:
            rec[r] += 1
    if rec["비추천"] / n >= 0.8:
        return "collapse"
    if n < 4:
        return "stalled"
    if rec["추천"] > 0 and rec["비추천"] > 0:
        return "good"
    return "improving"


# ── Request Models ────────────────────────────────────────────
class ReportRequest(BaseModel):
    route: str = "NSR"
    ice_class: str = "PC5"
    departure_date_start: str = ""  # YYYY-MM-DD, 빈 값이면 오늘
    forecast_days: int = 30
    transit_days: int = 14


class RLTrainRequest(BaseModel):
    curriculum: bool = True
    difficulty: str = "medium"
    timesteps: int = 100_000


class DepartureIterativeTrainRequest(BaseModel):
    ice_class: str = "PC5"
    forecast_days: int = 30
    transit_days: int = 14
    base_timesteps: int = 100_000
    max_iterations: int = 10
    target_success_rate: float = 0.80
    target_prohibitive_rate: float = 0.10
    eval_episodes: int = 50
    initial_weights: dict | None = None


class MultiModelTrainRequest(BaseModel):
    base_timesteps: int = 100_000
    max_iterations: int = 10
    target_success_rate: float = 0.80
    target_prohibitive_rate: float = 0.10
    eval_episodes: int = 50
    forecast_days: int = 30


class SarTrainRequest(BaseModel):
    epochs: int = 30
    batch_size: int = 4
    synthetic_count: int = 200
    device: str = "cpu"


class WhatIfRequest(BaseModel):
    route: str = "NSR"
    ice_class: str = "PC5"
    departure_date_start: str = ""
    forecast_days: int = 30


# ── 보고서 생성 파이프라인 ────────────────────────────────────
async def _generate_report(job_id: str, req: ReportRequest):
    """비동기 보고서 생성 파이프라인."""
    try:
        # 1. 데이터 로딩 (10%)
        _update_job(job_id, 5)
        monthly_summary = data_loader.build_monthly_summary()
        monthly_ice = data_loader.load_monthly_ice()
        latest_ice = data_loader.load_latest_ice()
        icebergs = data_loader.load_icebergs()
        weather = data_loader.load_weather()
        _update_job(job_id, 10)

        # 2. POLARIS 스코어링 (20%)
        start = (
            date.fromisoformat(req.departure_date_start)
            if req.departure_date_start
            else date.today()
        )
        calendar = route_scorer.build_departure_calendar(
            start, req.forecast_days, req.route, req.ice_class
        )
        all_scores = route_scorer.score_all_routes(
            start, req.forecast_days, req.ice_class
        )
        route_summary = route_scorer.get_route_summary(all_scores)
        _update_job(job_id, 20)

        # 3. RL(A) 인퍼런스 (30%)
        rl_departure_scores = {}
        if departure_agent.is_trained:
            for day_score in calendar:
                dep_date = date.fromisoformat(day_score.date)
                obs = _build_rl_obs(dep_date, monthly_ice, weather)
                if obs is not None:
                    action, _ = departure_agent.predict(obs)
                    if action is not None:
                        confidence = float((action[0] + 1) / 2)
                        rl_departure_scores[day_score.date] = round(confidence, 4)
        _update_job(job_id, 30)

        # 4. RL(C) 인퍼런스 (40%)
        rl_avoidance = {}
        try:
            rl_avoidance = await existing_rl_client.get_all_segment_difficulties(
                ice_data=latest_ice.get("stats", {}),
                icebergs=icebergs.get("bergs", []),
                weather={"visibility_km": 10, "wave_height_m": 1.5},
            )
        except Exception as e:
            logger.warning("RL(C) 인퍼런스 실패: %s", e)
        _update_job(job_id, 40)

        # 5. AI 분석 ×4 (40→75%)
        ai_monthly = trend_analyzer.analyze_monthly_trends(monthly_summary)
        _update_job(job_id, 50)

        ai_current = trend_analyzer.analyze_current_conditions(
            latest_ice.get("stats", {}),
            icebergs.get("stats", {}),
            weather,
        )
        _update_job(job_id, 58)

        weather_route = weather.get("routes", {}).get(req.route)
        ai_route = trend_analyzer.analyze_route_risk(
            req.route,
            route_summary.get(req.route, {}),
            weather_route,
        )
        _update_job(job_id, 66)

        ai_conclusions = trend_analyzer.write_conclusions(
            route_summary, req.ice_class, req.transit_days
        )
        _update_job(job_id, 75)

        # 6. PDF 생성 (75→100%)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_filename = f"arctic_report_{req.route}_{timestamp}.pdf"
        pdf_path = OUTPUT_DIR / pdf_filename

        pdf_generator.generate(
            output_path=pdf_path,
            route=req.route,
            ice_class=req.ice_class,
            departure_date_start=req.departure_date_start or start.isoformat(),
            forecast_days=req.forecast_days,
            transit_days=req.transit_days,
            monthly_summary=monthly_summary,
            latest_ice_stats=latest_ice.get("stats", {}),
            berg_stats=icebergs.get("stats", {}),
            weather_data=weather,
            calendar=calendar,
            all_scores=all_scores,
            route_summary=route_summary,
            ai_monthly=ai_monthly,
            ai_current=ai_current,
            ai_route=ai_route,
            ai_conclusions=ai_conclusions,
            rl_departure_scores=rl_departure_scores,
            rl_training_history=departure_trainer.training_history,
            rl_avoidance_difficulties=rl_avoidance,
            rl_model_info=departure_agent.get_metadata(),
            rl_calibration_info=calibrator.get_info(),
        )

        _complete_job(job_id, pdf_path)
        logger.info("보고서 생성 완료: %s", pdf_path)

    except Exception as e:
        logger.error("보고서 생성 실패: %s", e, exc_info=True)
        _fail_job(job_id, str(e))


def _build_rl_obs(dep_date, monthly_ice, weather):
    """RL(A) 관측 벡터 빌드 (간소화)."""
    try:
        import numpy as np
        from modules.rl.departure_env import DepartureSchedulingEnv
        env = DepartureSchedulingEnv(
            monthly_ice=monthly_ice,
            weather_data=weather,
            route_scorer=route_scorer,
        )
        obs = env._build_observation(0)
        env.close()
        return obs
    except Exception:
        return None


# ── RL 학습 파이프라인 ────────────────────────────────────────
rl_train_jobs: dict[str, dict] = {}


def _run_rl_training(job_id: str, req: RLTrainRequest):
    """RL(A) 학습 실행."""
    try:
        monthly_ice = data_loader.load_monthly_ice()
        weather = data_loader.load_weather()

        if req.curriculum:
            departure_trainer.train_curriculum(
                monthly_ice=monthly_ice,
                weather_data=weather,
                route_scorer=route_scorer,
                base_timesteps=req.timesteps,
            )
        else:
            departure_trainer.train_single(
                difficulty=req.difficulty,
                timesteps=req.timesteps,
                monthly_ice=monthly_ice,
                weather_data=weather,
                route_scorer=route_scorer,
            )

        # 모델 리로드
        departure_agent._try_load()
        if job_id in rl_train_jobs:
            rl_train_jobs[job_id]["status"] = "completed"
            rl_train_jobs[job_id]["progress"] = 100
    except Exception as e:
        logger.error(f"학습 실패 (Job {job_id}): {e}", exc_info=True)
        if job_id in rl_train_jobs:
            rl_train_jobs[job_id]["status"] = "failed"
            rl_train_jobs[job_id]["error"] = str(e)


# ── Endpoints ─────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Arctic Trend Report Service", "docs": "/docs"}


@app.get("/api/report/health")
async def health():
    return {
        "status": "ok",
        "rl_model_loaded": departure_agent.is_trained,
        "calibration_episodes": calibrator.state.get("episode_count", 0),
    }


@app.post("/api/report/generate")
async def generate_report(req: ReportRequest, bg: BackgroundTasks):
    """보고서 생성 시작 (비동기)."""
    job_id = _create_job()
    bg.add_task(_generate_report, job_id, req)
    return {"job_id": job_id, "message": "보고서 생성 시작"}


@app.get("/api/report/status/{job_id}")
async def report_status(job_id: str):
    """보고서 생성 진행률 조회."""
    if job_id not in jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return jobs[job_id]


@app.get("/api/report/download/{job_id}")
async def download_report(job_id: str):
    """생성된 PDF 다운로드."""
    if job_id not in jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    job = jobs[job_id]
    if job["status"] != "completed" or not job.get("pdf_path"):
        return JSONResponse(status_code=400, content={"error": "보고서가 아직 준비되지 않았습니다."})

    pdf_path = Path(job["pdf_path"])
    if not pdf_path.exists():
        return JSONResponse(status_code=404, content={"error": "PDF 파일을 찾을 수 없습니다."})

    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
    )


@app.post("/api/report/rl/train")
async def rl_train(req: RLTrainRequest, bg: BackgroundTasks):
    """RL(A) 출항 스케줄링 학습 시작."""
    if departure_trainer.is_training:
        return JSONResponse(status_code=409, content={"error": "이미 학습이 진행 중입니다."})

    job_id = str(uuid.uuid4())[:8]
    rl_train_jobs[job_id] = {"status": "running", "progress": 0}
    bg.add_task(_run_rl_training, job_id, req)
    return {"job_id": job_id, "message": "RL 학습 시작", "curriculum": req.curriculum}


@app.get("/api/report/rl/train-status/{job_id}")
async def rl_train_status(job_id: str):
    """RL 학습 진행률 조회."""
    if job_id in rl_train_jobs:
        info = rl_train_jobs[job_id].copy()
        info.update(departure_trainer.get_status())
        return info
    return JSONResponse(status_code=404, content={"error": "Job not found"})


@app.get("/api/report/rl/status")
async def rl_status_general():
    """전체 RL(A) 학습 상태 조회 (프론드엔트 HUD용)."""
    return departure_trainer.get_status()


@app.post("/api/report/rl/stop")
async def rl_stop():
    """진행 중인 RL(A) 학습 중단 요청 (반복 학습 포함)."""
    if not departure_trainer.is_training and not departure_iterative_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "학습 중이 아닙니다."})
    departure_trainer.stop_requested = True
    if departure_iterative_trainer.is_running:
        departure_iterative_trainer.stop_requested = True
    return {"message": "학습 중단 요청됨"}


@app.post("/api/report/rl/calibrate")
async def rl_calibrate():
    """RL(B) 예측 교정 실행."""
    # 현재 임계값으로 예측 RIO 계산
    monthly_ice = data_loader.load_monthly_ice()
    month = date.today().month
    cells = monthly_ice.get(month, {}).get("cells", [])

    if not cells:
        return JSONResponse(status_code=400, content={"error": "현재 월 해빙 데이터 없음"})

    from modules.route_scorer import ARCTIC_SEGMENTS, concentration_to_ice_conditions
    import sys as _sys
    # Digital_twin/backend/pipeline 우선, 단독 배포(HF) 폴더 폴백
    for _cand in [
        Path(__file__).resolve().parents[2] / "pipeline",  # backend/services/report-service → backend/pipeline (현재 레이아웃)
        Path(__file__).resolve().parents[2] / "backend" / "pipeline",
        Path(__file__).parent.parent / "backend" / "pipeline",
    ]:
        if _cand.exists():
            _router_path = str(_cand)
            if _router_path not in _sys.path:
                _sys.path.insert(0, _router_path)
            break
    from arctic_master_router import calculate_rio, IceCondition

    predicted_rios = []
    actual_rios = []

    segments = ARCTIC_SEGMENTS.get("NSR", [])
    for seg in segments:
        conc = route_scorer._get_segment_concentration(seg, cells)
        ice_conds = concentration_to_ice_conditions(conc)
        try:
            pred_rio = calculate_rio("PC5", cast(list[IceCondition], ice_conds))
            predicted_rios.append(pred_rio)
            # 실제 RIO는 현재 데이터 기반으로 근사 (작은 노이즈 추가)
            import numpy as np
            actual_rios.append(pred_rio + np.random.normal(0, 0.5))
        except Exception:
            predicted_rios.append(0)
            actual_rios.append(0)

    result = calibrator.calibrate(predicted_rios, actual_rios)
    return result


@app.get("/api/report/rl/model-info")
async def rl_model_info():
    """학습된 모델 메타데이터."""
    return {
        "departure_agent": departure_agent.get_metadata(),
        "calibrator": calibrator.get_info(),
        "trainer": departure_trainer.get_status(),
    }


# ── 출항 RL 반복 학습 Endpoints ───────────────────────────────
@app.post("/api/report/rl/departure/train/iterative")
async def departure_iterative_train(req: DepartureIterativeTrainRequest, bg: BackgroundTasks):
    """출항 RL 자동화 반복 학습 시작 — 학습→평가→보상 조정→재학습 루프."""
    if departure_trainer.is_training or departure_iterative_trainer.is_running:
        return JSONResponse(status_code=409, content={"error": "이미 학습이 진행 중입니다."})

    initial_weights = None
    if req.initial_weights:
        try:
            from modules.rl.departure_env import DepartureRewardWeights
            initial_weights = DepartureRewardWeights(**req.initial_weights)
        except Exception as e:
            return JSONResponse(status_code=400,
                                content={"error": f"initial_weights 형식 오류: {e}"})

    monthly_ice = data_loader.load_monthly_ice()
    weather = data_loader.load_weather()

    bg.add_task(
        departure_iterative_trainer.run,
        monthly_ice=monthly_ice,
        weather_data=weather,
        route_scorer=route_scorer,
        ice_class=req.ice_class,
        forecast_days=req.forecast_days,
        transit_days=req.transit_days,
        base_timesteps=req.base_timesteps,
        max_iterations=req.max_iterations,
        target_success_rate=req.target_success_rate,
        target_prohibitive_rate=req.target_prohibitive_rate,
        eval_episodes=req.eval_episodes,
        initial_weights=initial_weights,
    )
    return {"message": "출항 RL 반복 학습 시작",
            "max_iterations": req.max_iterations,
            "target_success_rate": req.target_success_rate,
            "target_prohibitive_rate": req.target_prohibitive_rate}


@app.get("/api/report/rl/departure/train/iterative/status")
async def departure_iterative_status():
    """출항 RL 반복 학습 진행 상태 조회."""
    return departure_iterative_trainer.get_status()


@app.post("/api/report/rl/departure/train/iterative/stop")
async def departure_iterative_stop():
    """출항 RL 반복 학습 중단 요청."""
    if not departure_iterative_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "반복 학습이 실행 중이 아닙니다."})
    departure_iterative_trainer.stop()
    return {"message": "출항 RL 반복 학습 중단 요청됨"}


# ── 다중 모델 (빙급 × 선종) 병렬 학습 Endpoints ───────────────
@app.post("/api/report/rl/multi/train")
async def multi_model_train(req: MultiModelTrainRequest, bg: BackgroundTasks):
    """빙급 × 선종 전체 조합을 동시에 반복 학습 시작."""
    if multi_model_trainer.is_running:
        return JSONResponse(status_code=409, content={"error": "이미 다중 모델 학습이 진행 중입니다."})

    monthly_ice = data_loader.load_monthly_ice()
    weather = data_loader.load_weather()

    bg.add_task(
        multi_model_trainer.start,
        monthly_ice=monthly_ice,
        weather_data=weather,
        route_scorer=route_scorer,
        base_timesteps=req.base_timesteps,
        max_iterations=req.max_iterations,
        target_success_rate=req.target_success_rate,
        target_prohibitive_rate=req.target_prohibitive_rate,
        eval_episodes=req.eval_episodes,
        forecast_days=req.forecast_days,
    )

    combos = [{"ice_class": ic, "ship_type": st,
                "ship_label": SHIP_TYPES[st]["label"]}
               for ic, st in ALL_COMBINATIONS]
    return {
        "message": f"다중 모델 학습 시작 ({len(ALL_COMBINATIONS)}개 조합)",
        "combinations": combos,
    }


@app.get("/api/report/rl/multi/status")
async def multi_model_status():
    """다중 모델 학습 진행 상태 조회."""
    return multi_model_trainer.get_status()


@app.post("/api/report/rl/multi/stop")
async def multi_model_stop():
    """다중 모델 학습 전체 중단."""
    if not multi_model_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "다중 모델 학습이 실행 중이 아닙니다."})
    multi_model_trainer.stop()
    return {"message": "다중 모델 학습 중단 요청됨"}


# ══════════════════════════════════════════════════════════════
# What-If 시나리오 분석 API
# ══════════════════════════════════════════════════════════════

async def _run_whatif(job_id: str, req: WhatIfRequest):
    """비동기 What-If 시나리오 생성 — async 함수로 이벤트 루프에서 직접 실행."""
    import time
    from dataclasses import asdict
    from modules.whatif_generator_max import parse_result_v3
    _t0 = time.monotonic()
    try:
        _update_job(job_id, 10)

        # generate_scenarios의 sync wrapper를 거치지 않고 직접 await
        # (Python 3.14 + nest_asyncio 호환성)
        whatif_generator.collected_route_summaries = []
        whatif_generator.tool_calls_count = 0

        # tool-call 루프 진행 상황을 10~85% 구간으로 실시간 반영 (체감 속도 개선)
        def _progress(pct: int):
            _update_job(job_id, max(10, min(85, pct)))

        ai_text = await whatif_generator._async_generate(
            route=req.route,
            ice_class=req.ice_class,
            departure_date=req.departure_date_start or date.today().isoformat(),
            forecast_days=req.forecast_days,
            progress_cb=_progress,
        )
        result = parse_result_v3(ai_text, whatif_generator.collected_route_summaries)
        result.tool_calls_count = whatif_generator.tool_calls_count

        _update_job(job_id, 90)
        result_dict = {
            "scenarios": [asdict(s) for s in result.scenarios],
            "comparison_text": result.comparison_text,
            "ai_recommendation": result.ai_recommendation,
            "tool_calls_count": result.tool_calls_count,
        }
        jobs[job_id]["result"] = result_dict
        _update_job(job_id, 100, status="completed")

        # 통계 누적 (/api/report/whatif/stats 가 집계)
        _save_whatif_run({
            "route": req.route,
            "ice_class": req.ice_class,
            "n_scenarios": len(result.scenarios),
            "iterations": result.tool_calls_count,
            "latency_ms": int((time.monotonic() - _t0) * 1000),
            "convergence": _convergence_status(result_dict["scenarios"]),
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
        logger.info("What-If 완료: %d 시나리오", len(result.scenarios))
    except Exception as e:
        logger.error("What-If 실패: %s", e, exc_info=True)
        _fail_job(job_id, str(e))


@app.post("/api/report/whatif")
async def start_whatif(req: WhatIfRequest):
    """What-If 시나리오 분석을 시작합니다."""
    job_id = _create_job()
    # BackgroundTasks 우회: asyncio.create_task로 직접 schedule
    asyncio.create_task(_run_whatif(job_id, req))
    return {"job_id": job_id, "message": "What-If 시나리오 분석 시작"}


@app.get("/api/report/whatif/status/{job_id}")
async def whatif_status(job_id: str):
    """What-If 분석 진행 상태 조회."""
    job = jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return {
        "status": job["status"],
        "progress": job["progress"],
        "error": job.get("error"),
        "result": job.get("result"),
    }


@app.get("/api/report/whatif/stats")
async def whatif_stats():
    """누적된 What-If 실행 통계 (WhatIfPanel 의 STATS 패널용)."""
    runs = _load_whatif_runs()
    n = len(runs)
    if n == 0:
        return {
            "n_runs": 0, "avg_iterations": 0, "avg_scenarios": 0,
            "avg_latency_ms": 0, "convergence_dist": {},
            "by_route": {}, "by_ice_class": {},
        }

    def _avg(key: str) -> float:
        return round(sum((r.get(key) or 0) for r in runs) / n, 1)

    conv: dict[str, int] = {}
    by_route: dict[str, int] = {}
    by_ice: dict[str, int] = {}
    for r in runs:
        conv[r.get("convergence", "n/a")] = conv.get(r.get("convergence", "n/a"), 0) + 1
        by_route[r.get("route", "?")] = by_route.get(r.get("route", "?"), 0) + 1
        by_ice[r.get("ice_class", "?")] = by_ice.get(r.get("ice_class", "?"), 0) + 1

    return {
        "n_runs": n,
        "avg_iterations": _avg("iterations"),
        "avg_scenarios": _avg("n_scenarios"),
        "avg_latency_ms": int(_avg("latency_ms")),
        "convergence_dist": conv,
        "by_route": by_route,
        "by_ice_class": by_ice,
    }


# ══════════════════════════════════════════════════════════════
# SAR 빙산 탐지 모델 학습 API — 8003 전용 서버로 프록시
# ══════════════════════════════════════════════════════════════
# SAR 학습은 sar_server.py (포트 8003) 에서 독립 프로세스로 실행됩니다.
# 이 서버(8002)는 RL 학습 전용으로 유지되어 이벤트루프 블로킹이 발생하지 않습니다.

SAR_SERVER_URL = "http://127.0.0.1:8003"


async def _sar_proxy(method: str, path: str, body: bytes | None = None) -> dict:
    """8003 SAR 서버로 요청을 프록시."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        if method == "GET":
            r = await client.get(f"{SAR_SERVER_URL}{path}")
        else:
            r = await client.post(f"{SAR_SERVER_URL}{path}", content=body,
                                  headers={"Content-Type": "application/json"})
        return r.json()


@app.post("/api/report/sar/train")
async def start_sar_training(req: SarTrainRequest):
    """SAR 학습 시작 — sar_server(8003)로 위임."""
    try:
        import json as _json
        result = await _sar_proxy("POST", "/api/sar/train", _json.dumps(req.dict()).encode())
        return result
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"error": "SAR 서버(8003)에 연결할 수 없습니다. 'python sar_server.py' 를 먼저 실행하세요."},
        )


@app.get("/api/report/sar/train-status")
async def sar_train_status():
    """SAR 학습 상태 — sar_server(8003)에서 조회."""
    try:
        return await _sar_proxy("GET", "/api/sar/status")
    except Exception:
        return {"error": "SAR 서버(8003) 응답 없음", "is_training": False}


@app.get("/api/report/sar/model-info")
async def sar_model_info():
    """SAR 모델 메타데이터 — sar_server(8003)에서 조회."""
    try:
        return await _sar_proxy("GET", "/api/sar/model-info")
    except Exception:
        return {"error": "SAR 서버(8003) 응답 없음"}
