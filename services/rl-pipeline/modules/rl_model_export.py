"""
rl_model_export.py -- 모델 내보내기 (PyTorch -> ONNX)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

EXPORT_DIR = Path(__file__).resolve().parent.parent / "models" / "export"


def export_to_onnx(agent, output_path: str | None = None) -> str | None:
    try:
        import torch
    except ImportError:
        logger.error("PyTorch가 필요합니다: pip install torch")
        return None

    if agent.model is None:
        logger.error("모델이 로드되지 않았습니다.")
        return None

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    onnx_path = output_path or str(EXPORT_DIR / "sac_actor.onnx")

    try:
        actor = agent.model.policy.actor
        dummy_input = torch.randn(1, 22)

        torch.onnx.export(
            actor, (dummy_input,), onnx_path,
            input_names=["observation"],
            output_names=["mean_action", "log_std"],
            dynamic_axes={
                "observation": {0: "batch_size"},
                "mean_action": {0: "batch_size"},
                "log_std": {0: "batch_size"},
            },
            opset_version=11,
        )

        logger.info(f"[Export] ONNX 모델 저장: {onnx_path}")
        _save_normalization_constants(EXPORT_DIR / "normalization.json")
        return onnx_path

    except Exception as e:
        logger.error(f"[Export] ONNX 내보내기 실패: {e}", exc_info=True)
        return None


def _save_normalization_constants(path: Path):
    constants = {
        "obs_dims": 22, "action_dims": 2,
        "obs_normalization": {
            "lon_scale": 180.0, "lat_scale": 90.0, "speed_scale": 15.0,
            "distance_scale_wp": 100.0, "distance_scale_berg": 50.0,
            "bearing_scale": 180.0, "visibility_scale": 20.0,
            "wave_scale": 8.0, "deviation_scale": 50.0,
        },
        "action_ranges": {
            "heading_delta": {"min": -15.0, "max": 15.0},
            "speed_factor": {"min": 0.5, "max": 1.0},
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(constants, f, indent=2)
