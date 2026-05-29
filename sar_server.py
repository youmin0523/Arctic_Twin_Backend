"""
sar_server.py — SAR YOLOv8 빙산 탐지 전용 서버 (포트 8003)

RL 서버(8001, 8002)와 완전히 분리된 프로세스로 실행.
SAR 학습이 이벤트루프를 오래 점유하더라도 RL 서버에 영향 없음.

v2: 1회 학습 완료 후 자동 반복 개선 (IcebergIterativeTrainer 통합)

Run: python sar_server.py
"""
import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# backend pipeline trainers 경로 추가 (sar_server.py 는 backend/ 루트에 위치)
_BASE = Path(__file__).parent
_PIPELINE_TRAINERS = _BASE / "pipeline" / "trainers"
if str(_PIPELINE_TRAINERS) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_TRAINERS))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sar-server")

app = FastAPI(
    title="SAR Iceberg Detection Training Server",
    description="YOLOv8 빙산 탐지 모델 자동 반복 개선 서버 (포트 8003)",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 상태 ──────────────────────────────────────────────────────
_state: dict = {
    "is_training":    False,
    "progress":       0,
    "stage":          "대기 중",
    "result":         None,
    "error":          None,
    "started_at":     None,
    "finished_at":    None,
    "trained_at":     None,
    # 반복 학습 추가 필드
    "mode":           "idle",   # idle | single | iterative
    "iteration":      0,
    "max_iterations": 0,
    "metrics":        {},
    "iterative_status": {},
}


class SarTrainRequest(BaseModel):
    epochs: int = 30
    batch_size: int = 4
    synthetic_count: int = 200
    device: str = "cpu"
    max_iterations: int = 3          # 자동 반복 최대 횟수 (1 = 단순 1회)
    force_restart: bool = False


# ── 상태 콜백 ─────────────────────────────────────────────────
def _status_cb(stage: str, progress: int, **extra):
    _state["stage"]    = stage
    _state["progress"] = progress
    if "metrics" in extra:
        _state["metrics"] = extra["metrics"]
    if "signals" in extra:
        _state["iterative_status"]["last_signals"] = extra["signals"]


# ── 반복 학습 (스레드에서 실행) ───────────────────────────────
def _iterative_train_sync(req: SarTrainRequest) -> None:
    _state.update(
        is_training=True, progress=0, error=None, result=None,
        stage="SAR 반복 학습 초기화 중...",
        started_at=datetime.now().isoformat(), finished_at=None,
        mode="iterative" if req.max_iterations > 1 else "single",
        iteration=0, max_iterations=req.max_iterations,
        iterative_status={},
    )
    try:
        from iceberg_iterative_trainer import IcebergIterativeTrainer

        trainer = IcebergIterativeTrainer(status_callback=_status_cb)

        # 초기 파라미터 오버라이드 (요청 파라미터 반영)
        if trainer._history == [] or req.force_restart:
            trainer._history = []
            # 첫 반복 파라미터를 요청값으로 설정
            trainer._initial_override = {
                "epochs":          req.epochs,
                "batch_size":      req.batch_size,
                "synthetic_count": req.synthetic_count,
                "device":          req.device,
                "img_size":        640,
                "hsv_v":           0.3,
                "flipud":          0.5,
                "fliplr":          0.5,
                "degrees":         180,
                "scale":           0.3,
                "mosaic":          0.5,
            }

        final = trainer.run(
            max_iterations=req.max_iterations,
            force_restart=req.force_restart,
        )

        _state.update(
            is_training=False, progress=100,
            stage=("수렴 완료" if final.get("converged")
                   else f"반복 완료 ({final['total_iterations']}회)"),
            result=final,
            metrics=final.get("best_metrics", {}),
            trained_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(),
            iterative_status=trainer.get_status(),
        )
        logger.info("SAR 반복 학습 완료: %d회, 최고 mAP50=%.4f",
                    final["total_iterations"],
                    final.get("best_metrics", {}).get("mAP50") or 0.0)

    except Exception as e:
        logger.error("SAR 반복 학습 실패: %s", e, exc_info=True)
        _state.update(
            is_training=False, stage="오류 발생", error=str(e),
            finished_at=datetime.now().isoformat(),
        )


# ── 엔드포인트 ────────────────────────────────────────────────

@app.get("/")
async def health():
    return {
        "service": "SAR Training Server",
        "port": 8005,
        "is_training": _state["is_training"],
        "mode": _state["mode"],
    }


@app.post("/api/sar/train")
async def start_training(req: SarTrainRequest, bg: BackgroundTasks):
    """SAR YOLOv8 학습 시작.
    max_iterations > 1 이면 자동 반복 개선 모드로 실행됩니다."""
    if _state["is_training"]:
        return JSONResponse(status_code=409, content={"error": "이미 SAR 학습 진행 중"})
    bg.add_task(
        lambda: threading.Thread(
            target=_iterative_train_sync, args=(req,), daemon=True
        ).start()
    )
    mode = "자동 반복 개선" if req.max_iterations > 1 else "단일"
    return {
        "message": f"SAR YOLOv8 {mode} 학습 시작",
        "epochs": req.epochs,
        "synthetic_count": req.synthetic_count,
        "device": req.device,
        "max_iterations": req.max_iterations,
    }


@app.get("/api/sar/status")
async def get_status():
    return _state


@app.get("/api/sar/iterative/status")
async def iterative_status():
    """반복 학습 히스토리 조회."""
    hist_path = _BASE / "pipeline" / "models" / "iceberg_iterative_history.json"
    if hist_path.exists():
        try:
            return json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"iterations": [], "message": "히스토리 없음"}


@app.get("/api/sar/model-info")
async def model_info():
    meta_path = _BASE / "pipeline" / "models" / "iceberg_yolov8_meta.json"
    if not meta_path.exists():
        return JSONResponse(status_code=404, content={"error": "모델 메타데이터 없음"})
    return json.loads(meta_path.read_text(encoding="utf-8"))


# ── 실행 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="info")
