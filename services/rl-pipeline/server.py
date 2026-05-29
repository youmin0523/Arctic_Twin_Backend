"""
RL Iceberg Avoidance Pipeline -- FastAPI Server

Run: uvicorn server:app --reload --port 8001
(포트 8001: 기존 백엔드 8000과 충돌 방지)
"""
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List
import asyncio
import logging

# ── Python 3.14 + anyio/starlette 호환성 패치 ─────────────────────
import anyio.to_thread as _ato  # noqa: E402
import starlette.background as _sb  # noqa: E402


async def _patched_anyio_to_thread_run_sync(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args)


_ato.run_sync = _patched_anyio_to_thread_run_sync


async def _patched_bg_call(self):
    if self.is_async:
        await self.func(*self.args, **self.kwargs)
    else:
        await asyncio.to_thread(self.func, *self.args, **self.kwargs)


_sb.BackgroundTask.__call__ = _patched_bg_call

from modules.rl_trainer import RLTrainer
from modules.rl_iterative_trainer import IterativeTrainer
from modules.rl_multi_model_trainer import RLMultiModelTrainer, ALL_COMBINATIONS, SHIP_TYPES, ROUTES, ICE_CLASSES
from modules.rl_reward import RewardWeights

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rl-pipeline")

app = FastAPI(
    title="RL Iceberg Avoidance Pipeline",
    description="빙산 회피 강화학습 API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── RL 트레이너 초기화 ────────────────────────────────────
rl_trainer = RLTrainer()
iterative_trainer = IterativeTrainer(base_trainer=rl_trainer)
multi_model_trainer = RLMultiModelTrainer()


# ── Request Models ────────────────────────────────────────
class RLInferRequest(BaseModel):
    ship_state: dict        # {lon, lat, heading, speed_knots, ice_class, progress}
    icebergs: List[dict]    # [{lat, lon, length_m}, ...]
    ice_data: dict          # {concentration: float}
    weather: dict           # {visibility_km, wave_height_m}


class RLTrainRequest(BaseModel):
    difficulty: str = "medium"
    timesteps: int = 100_000
    curriculum: bool = False


class IterativeTrainRequest(BaseModel):
    max_iterations: int = 5
    target_success_rate: float = 0.70
    target_collision_rate: float = 0.15
    eval_episodes: int = 30
    eval_difficulty: str = "medium"
    initial_weights: dict | None = None


class MultiModelTrainRequest(BaseModel):
    max_iterations: int = 5
    target_success_rate: float = 0.70
    target_collision_rate: float = 0.15
    eval_episodes: int = 30
    eval_difficulty: str = "medium"
    base_timesteps: int = 150_000


# ── Endpoints ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "RL Iceberg Avoidance Pipeline", "docs": "/docs"}


@app.get("/api/rl/health")
async def health():
    return {"status": "ok", "model_loaded": rl_trainer.agent.model is not None}


@app.post("/api/rl/infer")
async def rl_infer(req: RLInferRequest):
    """RL 실시간 추론 — 선박 상태와 빙산 정보를 받아 회피 행동 반환"""
    try:
        result = rl_trainer.infer(
            ship_state=req.ship_state,
            icebergs=req.icebergs,
            ice_data=req.ice_data,
            weather=req.weather,
        )
        return result
    except Exception as e:
        logger.error(f"[RL] 추론 실패: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e), "fallback": True})


@app.post("/api/rl/train")
async def rl_train(req: RLTrainRequest, bg: BackgroundTasks):
    """RL 학습 시작 (비동기)"""
    logger.info(f"[API] RL 학습 요청: {req}")
    try:
        if rl_trainer.is_training:
            return JSONResponse(status_code=409, content={"error": "이미 학습이 진행 중입니다."})

        if req.curriculum:
            bg.add_task(rl_trainer.train_curriculum)
        else:
            bg.add_task(rl_trainer.train_single, req.difficulty, req.timesteps)

        return {"message": "학습 시작", "curriculum": req.curriculum,
                "difficulty": req.difficulty, "timesteps": req.timesteps}
    except Exception as e:
        logger.error(f"[API] 학습 시작 실패: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/rl/status")
async def rl_status():
    """학습 상태 및 메트릭 조회"""
    return rl_trainer.get_status()


@app.post("/api/rl/stop")
async def rl_stop():
    """진행 중인 학습 중단 요청 (반복 학습 포함)"""
    if not rl_trainer.is_training and not iterative_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "학습 중이 아닙니다."})
    rl_trainer.stop_requested = True
    if iterative_trainer.is_running:
        iterative_trainer.stop_requested = True
    return {"message": "학습 중단 요청됨"}


@app.post("/api/rl/evaluate")
async def rl_evaluate(n_episodes: int = 100, difficulty: str = "medium"):
    """학습된 모델 평가"""
    return rl_trainer.evaluate(n_episodes=n_episodes, difficulty=difficulty)


# ── 반복 학습 Endpoints ────────────────────────────────────
@app.post("/api/rl/train/iterative")
async def rl_train_iterative(req: IterativeTrainRequest, bg: BackgroundTasks):
    """자동화 반복 학습 시작 — 학습→평가→보상 조정→재학습 루프"""
    if rl_trainer.is_training or iterative_trainer.is_running:
        return JSONResponse(status_code=409, content={"error": "이미 학습이 진행 중입니다."})

    initial_weights = None
    if req.initial_weights:
        try:
            initial_weights = RewardWeights(**req.initial_weights)
        except Exception as e:
            return JSONResponse(status_code=400,
                                content={"error": f"initial_weights 형식 오류: {e}"})

    bg.add_task(
        iterative_trainer.run,
        max_iterations=req.max_iterations,
        target_success_rate=req.target_success_rate,
        target_collision_rate=req.target_collision_rate,
        eval_episodes=req.eval_episodes,
        eval_difficulty=req.eval_difficulty,
        initial_weights=initial_weights,
    )
    return {"message": "반복 학습 시작",
            "max_iterations": req.max_iterations,
            "target_success_rate": req.target_success_rate,
            "target_collision_rate": req.target_collision_rate}


@app.get("/api/rl/train/iterative/status")
async def rl_iterative_status():
    """반복 학습 진행 상태 조회"""
    return iterative_trainer.get_status()


@app.post("/api/rl/train/iterative/stop")
async def rl_iterative_stop():
    """반복 학습 중단 요청"""
    if not iterative_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "반복 학습이 실행 중이 아닙니다."})
    iterative_trainer.stop()
    return {"message": "반복 학습 중단 요청됨"}


# ── 다중 모델 (항로 × 빙급 × 선종) 병렬 학습 Endpoints ──────
@app.post("/api/rl/multi/train")
async def rl_multi_train(req: MultiModelTrainRequest, bg: BackgroundTasks):
    """항로 × 빙급 × 선종 전체 조합을 동시에 반복 학습 시작."""
    if multi_model_trainer.is_running:
        return JSONResponse(status_code=409, content={"error": "이미 다중 모델 학습이 진행 중입니다."})

    bg.add_task(
        multi_model_trainer.start,
        max_iterations=req.max_iterations,
        target_success_rate=req.target_success_rate,
        target_collision_rate=req.target_collision_rate,
        eval_episodes=req.eval_episodes,
        eval_difficulty=req.eval_difficulty,
        base_timesteps=req.base_timesteps,
    )

    combos = [
        {"route": r, "ice_class": ic, "ship_type": st, "ship_label": SHIP_TYPES[st]["label"]}
        for r, ic, st in ALL_COMBINATIONS
    ]
    return {
        "message": f"다중 모델 학습 시작 ({len(ALL_COMBINATIONS)}개 조합)",
        "routes": ROUTES,
        "ice_classes": ICE_CLASSES,
        "ship_types": {k: v["label"] for k, v in SHIP_TYPES.items()},
        "combinations": combos,
    }


@app.get("/api/rl/multi/status")
async def rl_multi_status():
    """다중 모델 학습 진행 상태 조회."""
    return multi_model_trainer.get_status()


@app.post("/api/rl/multi/stop")
async def rl_multi_stop():
    """다중 모델 학습 전체 중단."""
    if not multi_model_trainer.is_running:
        return JSONResponse(status_code=400, content={"error": "다중 모델 학습이 실행 중이 아닙니다."})
    multi_model_trainer.stop()
    return {"message": "다중 모델 학습 중단 요청됨"}


# ── 실행 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
