"""
monitor.py — 강화학습 실시간 진행 모니터
실행: python monitor.py
"""
import os, sys, json, glob, time, re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import request, error as url_error


def _tail(seq: Any, n: int) -> "deque[Any]":
    """리스트/Any에서 마지막 n개를 반환 (슬라이싱 타입 오류 우회)."""
    return deque(seq, maxlen=n)

BASE = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트
RL_MODELS     = BASE / "backend/services/rl-pipeline" / "models"
REPORT_DATA   = BASE / "backend/services/report-service" / "data"
SAR_META      = BASE / "backend" / "pipeline" / "models" / "iceberg_yolov8_meta.json"
SAR_ITER_HIST = BASE / "backend" / "pipeline" / "models" / "iceberg_iterative_history.json"
FUEL_ITER_HIST = BASE / "backend/services/ml-pipeline" / "models" / "fuel_iterative_history.json"
ML_STATE_FILE  = BASE / "backend/services/ml-pipeline" / "models" / "training_state.json"
WHATIF_ITER_HIST = BASE / "backend/services/report-service" / "data" / "whatif_iterative_history.json"

MAX_ITER_RL   = 15      # rl-pipeline 목표 반복 수
MAX_ITER_DEP  = 15      # report-service 목표 반복 수
MAX_ITER_SAR  = 3       # SAR YOLOv8 목표 반복 수
MAX_ITER_FUEL = 5       # Fuel XGBoost 목표 반복 수
MAX_ITER_WHATIF = 3     # What-if 목표 반복 수
STEPS_PER_ITER_RL  = 500_000   # easy 100k + medium 200k + hard 200k
STEPS_PER_ITER_DEP = 250_000   # easy 50k + medium 100k + hard 100k
REFRESH = 15            # 화면 갱신 주기 (초)

# ── 색상 코드 ─────────────────────────────────────────────────
R  = "\033[91m"
G  = "\033[92m"
Y  = "\033[93m"
B  = "\033[94m"
C  = "\033[96m"
W  = "\033[97m"
DIM = "\033[2m"
RST = "\033[0m"
BOLD = "\033[1m"

def bar(ratio: float, width: int = 24) -> str:
    filled = int(ratio * width)
    pct = ratio * 100
    color = G if pct >= 70 else Y if pct >= 30 else R
    return f"{color}{'█' * filled}{'░' * (width - filled)}{RST} {pct:5.1f}%"

def eta_str(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds + td.days * 86400, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fetch_api(url: str) -> dict | None:
    try:
        with request.urlopen(url, timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None

# ── 체크포인트에서 현재 스텝 추정 ─────────────────────────────
def latest_ckpt_step(ckpt_dir: Path) -> int:
    """sac_ckpt_NNNNN_steps.zip 중 가장 큰 N 반환."""
    if not ckpt_dir.exists():
        return 0
    best = 0
    for f in ckpt_dir.iterdir():
        m = re.search(r'(\d+)_steps', f.name)
        if m:
            best = max(best, int(m.group(1)))
    return best

def ckpt_mtime(ckpt_dir: Path) -> float:
    """체크포인트 폴더에서 가장 최근 파일 mtime."""
    if not ckpt_dir.exists():
        return 0.0
    mtimes = [f.stat().st_mtime for f in ckpt_dir.iterdir() if f.is_file()]
    return max(mtimes) if mtimes else 0.0

# ── rl-pipeline 상태 읽기 ─────────────────────────────────────
def read_rl_pipeline():
    """84개 모델 상태를 파일 + API에서 읽기."""
    api = fetch_api("http://127.0.0.1:8001/api/rl/multi/status")
    api_models = (api or {}).get("models", {})
    server_alive = api is not None

    history_files = list(RL_MODELS.glob("iterative_history_*.json"))
    models = {}

    for hf in history_files:
        key = hf.stem.replace("iterative_history_", "")
        try:
            history = json.loads(hf.read_text(encoding="utf-8"))
        except Exception:
            history = []

        completed_iter = len(history)
        converged = history[-1].get("converged", False) if history else False

        # 평균 반복 시간 (완료된 것 기준)
        durations = [h.get("duration_seconds", 0) for h in history if h.get("duration_seconds", 0) > 0]
        avg_dur = sum(durations) / len(durations) if durations else None

        # 체크포인트로 현재 스텝 추정
        ckpt_dir = RL_MODELS / f"sac_{key}" / "checkpoints"
        cur_step = latest_ckpt_step(ckpt_dir)
        ckpt_mt  = ckpt_mtime(ckpt_dir)

        # API에서 is_running 가져오기
        api_info   = api_models.get(key, {})
        is_running = api_info.get("is_running", False)

        # 현재 반복이 진행 중이면 스텝 기반 진행률 계산
        # (완료된 iter 이후의 스텝은 현재 iter 내 진행)
        within_step = cur_step % STEPS_PER_ITER_RL if cur_step > 0 else 0
        within_ratio = min(within_step / STEPS_PER_ITER_RL, 1.0) if within_step else 0.0

        # 현재 stage 추정
        if within_step < 100_000:
            stage = "easy"
        elif within_step < 300_000:
            stage = "medium"
        else:
            stage = "hard"

        # ETA 계산
        remaining_iter = MAX_ITER_RL - completed_iter
        eta_sec = None
        if avg_dur and remaining_iter > 0:
            eta_sec = avg_dur * remaining_iter - (within_ratio * avg_dur)

        models[key] = {
            "completed_iter": completed_iter,
            "converged": converged,
            "is_running": is_running,
            "within_ratio": within_ratio,
            "within_step": within_step,
            "stage": stage,
            "eta_sec": eta_sec,
            "ckpt_mtime": ckpt_mt,
            "avg_dur": avg_dur,
        }

    return server_alive, models

# ── report-service 상태 읽기 ─────────────────────────────────
def read_sar_server():
    """SAR 서버(8003) 상태 조회."""
    api = fetch_api("http://127.0.0.1:8003/api/sar/status")
    server_alive = api is not None
    if not server_alive:
        # 서버 없어도 메타 파일로 마지막 학습 정보 표시
        if SAR_META.exists():
            try:
                meta = json.loads(SAR_META.read_text(encoding="utf-8"))
                return False, {
                    "is_training": False, "progress": 100,
                    "stage": f"완료 (epochs={meta.get('epochs','?')})",
                    "trained_at": meta.get("trained_at", ""),
                    "error": None,
                }
            except Exception:
                pass
        return False, None
    return True, api


def read_sar_iterative():
    """SAR YOLOv8 반복 학습 히스토리 + API 상태 읽기."""
    api = fetch_api("http://127.0.0.1:8003/api/sar/status")
    server_alive = api is not None

    hist: list[Any] = []
    if SAR_ITER_HIST.exists():
        try:
            hist = list(json.loads(SAR_ITER_HIST.read_text(encoding="utf-8")))
        except Exception:
            hist = []

    state = api or {}
    return server_alive, state, hist


def read_ml_training():
    """ML Training Service(8004) — Fuel + What-if 상태 읽기."""
    api = fetch_api("http://127.0.0.1:8004/api/ml/status")
    server_alive = api is not None

    # 디스크 fallback
    fuel_hist: list[Any] = []
    whatif_hist: list[Any] = []
    if FUEL_ITER_HIST.exists():
        try:
            fuel_hist = json.loads(FUEL_ITER_HIST.read_text(encoding="utf-8"))
        except Exception:
            pass
    if WHATIF_ITER_HIST.exists():
        try:
            whatif_hist = json.loads(WHATIF_ITER_HIST.read_text(encoding="utf-8"))
        except Exception:
            pass

    fuel_state  = (api or {}).get("fuel", {})
    whatif_state = (api or {}).get("whatif", {})

    # API가 없으면 디스크 히스토리로 보정
    if not fuel_state and fuel_hist:
        best = max(fuel_hist, key=lambda r: r.get("metrics", {}).get("R2", 0))
        fuel_state = {
            "is_training": False,
            "stage": f"완료 (iter {len(fuel_hist)})",
            "metrics": best.get("metrics", {}),
            "iteration": len(fuel_hist),
            "max_iterations": MAX_ITER_FUEL,
            "mode": "iterative",
        }
    if not whatif_state and whatif_hist:
        best_w = max(whatif_hist, key=lambda r: r.get("quality", {}).get("scenarios_count", 0))
        whatif_state = {
            "is_running": False,
            "stage": f"완료 (iter {len(whatif_hist)})",
            "iteration": len(whatif_hist),
            "max_iterations": MAX_ITER_WHATIF,
            "scenarios_count": best_w.get("quality", {}).get("scenarios_count", 0),
            "mode": "iterative",
        }

    return server_alive, fuel_state, whatif_state, fuel_hist, whatif_hist


def read_report_service():
    api = fetch_api("http://127.0.0.1:8002/api/report/rl/multi/status")
    server_alive = api is not None
    api_models = (api or {}).get("models", {})

    history_files = list(REPORT_DATA.glob("departure_iterative_history_*.json"))
    models = {}
    ckpt_dir_base = REPORT_DATA / "departure_rl_model" / "checkpoints"

    # 전체 공유 체크포인트 (출항 RL은 모델별 별도 ckpt 없음 — 공유)
    shared_step = latest_ckpt_step(ckpt_dir_base)
    shared_mt   = ckpt_mtime(ckpt_dir_base)

    for hf in history_files:
        key = hf.stem.replace("departure_iterative_history_", "")
        try:
            history = json.loads(hf.read_text(encoding="utf-8"))
        except Exception:
            history = []

        completed_iter = len(history)
        converged = history[-1].get("converged", False) if history else False
        durations = [h.get("duration_seconds", 0) for h in history if h.get("duration_seconds", 0) > 0]
        avg_dur = sum(durations) / len(durations) if durations else None

        api_key = key.replace("_", " ", 1) if "IA_Super" not in key else key
        api_info   = api_models.get(key, {})
        is_running = api_info.get("is_running", False)

        within_step  = shared_step % STEPS_PER_ITER_DEP if shared_step > 0 else 0
        within_ratio = min(within_step / STEPS_PER_ITER_DEP, 1.0) if within_step else 0.0

        if within_step < 50_000:
            stage = "easy"
        elif within_step < 150_000:
            stage = "medium"
        else:
            stage = "hard"

        remaining_iter = MAX_ITER_DEP - completed_iter
        eta_sec = None
        if avg_dur and remaining_iter > 0:
            eta_sec = avg_dur * remaining_iter - (within_ratio * avg_dur)

        models[key] = {
            "completed_iter": completed_iter,
            "converged": converged,
            "is_running": is_running,
            "within_ratio": within_ratio,
            "within_step": within_step,
            "stage": stage,
            "eta_sec": eta_sec,
            "ckpt_mtime": shared_mt,
            "avg_dur": avg_dur,
        }

    return server_alive, models

# ── 화면 그리기 ────────────────────────────────────────────────
def render():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"\033[2J\033[H")   # 화면 클리어 + 커서 홈
    lines.append(f"{BOLD}{C}{'═'*72}{RST}")
    lines.append(f"{BOLD}{C}  강화학습 실시간 모니터   {DIM}{now}{RST}")
    lines.append(f"{BOLD}{C}{'═'*72}{RST}")

    # ── rl-pipeline ──────────────────────────────────────────
    rl_alive, rl_models = read_rl_pipeline()
    status_txt = f"{G}ALIVE{RST}" if rl_alive else f"{R}HTTP FROZEN (스레드는 실행중){RST}"
    lines.append(f"\n{BOLD}{W}[ RL-PIPELINE  포트 8001 ]  서버: {status_txt}{RST}")
    lines.append(f"{DIM}  빙산회피 SAC  84 노선 × 최대 {MAX_ITER_RL}회 반복{RST}\n")

    if rl_models:
        total_done = sum(m["completed_iter"] for m in rl_models.values())
        total_goal = len(rl_models) * MAX_ITER_RL
        converged  = sum(1 for m in rl_models.values() if m["converged"])
        overall    = total_done / total_goal if total_goal else 0

        lines.append(f"  전체 진행: {bar(overall, 30)}  ({total_done}/{total_goal} iter, 수렴 {converged}개)")

        # 현재 활발하게 움직이는 모델 (ckpt 최근 수정 기준 상위 6개)
        now_ts = time.time()
        active = sorted(
            [(k, v) for k, v in rl_models.items()
             if v["ckpt_mtime"] > 0 and now_ts - v["ckpt_mtime"] < 3600],
            key=lambda x: x[1]["ckpt_mtime"], reverse=True
        )[:6]

        if active:
            lines.append(f"\n  {BOLD}현재 학습 중 (체크포인트 최신 기준){RST}")
            lines.append(f"  {'모델':<30} {'반복':^10} {'스텝진행':^30} {'스테이지':^8} {'ETA':>10}")
            lines.append(f"  {'─'*30} {'─'*10} {'─'*30} {'─'*8} {'─'*10}")
            for key, m in active:
                iter_txt = f"{m['completed_iter']}/{MAX_ITER_RL}"
                step_bar = bar(m["within_ratio"], 18)
                eta = eta_str(m["eta_sec"]) if m["eta_sec"] else "계산중"
                stage_col = Y if m["stage"] == "medium" else R if m["stage"] == "hard" else G
                lines.append(f"  {key:<30} {iter_txt:^10} {step_bar}  {stage_col}{m['stage']:^8}{RST} {eta:>10}")

        # 완료된 모델 요약
        fully_done = [k for k, v in rl_models.items() if v["completed_iter"] >= MAX_ITER_RL]
        if fully_done:
            lines.append(f"\n  {G}완료된 모델: {len(fully_done)}개{RST}")
    else:
        lines.append(f"  {Y}히스토리 파일 없음 — 아직 첫 iter 진행중{RST}")

    # ── report-service ────────────────────────────────────────
    rp_alive, rp_models = read_report_service()
    status_txt2 = f"{G}ALIVE{RST}" if rp_alive else f"{R}HTTP FROZEN (스레드는 실행중){RST}"
    lines.append(f"\n{BOLD}{W}[ REPORT-SERVICE  포트 8002 ]  서버: {status_txt2}{RST}")
    lines.append(f"{DIM}  출항 스케줄링 SAC  28 조합 × 최대 {MAX_ITER_DEP}회 반복  (4개씩 배치){RST}\n")

    if rp_models:
        total_done2 = sum(m["completed_iter"] for m in rp_models.values())
        total_goal2 = len(rp_models) * MAX_ITER_DEP
        overall2    = total_done2 / total_goal2 if total_goal2 else 0
        lines.append(f"  전체 진행: {bar(overall2, 30)}  ({total_done2}/{total_goal2} iter)")

        now_ts = time.time()
        active2 = sorted(
            [(k, v) for k, v in rp_models.items()
             if v["ckpt_mtime"] > 0 and now_ts - v["ckpt_mtime"] < 3600],
            key=lambda x: x[1]["ckpt_mtime"], reverse=True
        )[:4]

        if active2:
            lines.append(f"\n  {BOLD}현재 학습 중{RST}")
            lines.append(f"  {'모델':<28} {'반복':^10} {'스텝진행':^30} {'ETA':>10}")
            lines.append(f"  {'─'*28} {'─'*10} {'─'*30} {'─'*10}")
            for key, m in active2:
                iter_txt = f"{m['completed_iter']}/{MAX_ITER_DEP}"
                step_bar = bar(m["within_ratio"], 18)
                eta = eta_str(m["eta_sec"]) if m["eta_sec"] else "계산중"
                lines.append(f"  {key:<28} {iter_txt:^10} {step_bar}  {eta:>10}")
    else:
        lines.append(f"  {Y}히스토리 파일 없음 — 아직 첫 iter 진행중{RST}")

    # ── SAR 서버 (반복 학습) ──────────────────────────────────
    sar_alive, sar, sar_hist = read_sar_iterative()
    sar_status_txt = f"{G}ALIVE{RST}" if sar_alive else f"{DIM}미실행{RST}"
    lines.append(f"\n{BOLD}{W}[ SAR-SERVER  포트 8003 ]  서버: {sar_status_txt}{RST}")
    lines.append(f"{DIM}  YOLOv8 빙산 탐지 딥러닝  (자동 반복 최대 {MAX_ITER_SAR}회){RST}\n")

    if sar or sar_hist:
        sar_stage    = str(sar.get("stage", ""))
        sar_prog     = int(sar.get("progress", 0) or 0)
        sar_err      = sar.get("error")
        sar_cur_iter = int(sar.get("iteration", len(sar_hist)) or 0)
        sar_max_iter = int(sar.get("max_iterations", MAX_ITER_SAR) or MAX_ITER_SAR)
        sar_metrics  = dict(sar.get("metrics", {}) or {})
        sar_trained  = str(sar.get("trained_at", "") or "")

        if sar_err:
            lines.append(f"  {R}에러: {sar_err}{RST}")
        else:
            sar_ratio = sar_cur_iter / max(sar_max_iter, 1)
            lines.append(f"  반복 진행: {bar(sar_ratio, 22)}  ({sar_cur_iter}/{sar_max_iter} iter)")
            if 0 < sar_prog < 100:
                lines.append(f"  현재 작업: {bar(sar_prog / 100, 22)}  {sar_stage}")
            elif "완료" in sar_stage or "수렴" in sar_stage:
                s_map50 = float(sar_metrics.get("mAP50") or 0.0)
                s_prec  = float(sar_metrics.get("precision") or 0.0)
                s_rec   = float(sar_metrics.get("recall") or 0.0)
                lines.append(f"  {G}학습 완료{RST}  mAP50={s_map50:.4f}  P={s_prec:.4f}  R={s_rec:.4f}  {DIM}{sar_trained}{RST}")

        # 히스토리 요약
        if sar_hist:
            lines.append(f"\n  {'iter':^6} {'mAP50':^8} {'Prec':^8} {'Recall':^8} {'시그널':<20} {'개선':^5}")
            lines.append(f"  {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*20} {'─'*5}")
            for rec in _tail(sar_hist, 3):
                rm    = dict(rec.get("metrics", {}) or {})
                sigs  = str(",".join(list(rec.get("signals", []))))[:18]
                imp   = f"{G}↑{RST}" if rec.get("improved") else f"{DIM}─{RST}"
                conv  = f" {G}✓수렴{RST}" if rec.get("converged") else ""
                lines.append(
                    f"  {int(rec['iteration']):^6} "
                    f"{float(rm.get('mAP50') or 0):^8.4f} "
                    f"{float(rm.get('precision') or 0):^8.4f} "
                    f"{float(rm.get('recall') or 0):^8.4f} "
                    f"{sigs:<20} {imp}{conv}"
                )
    else:
        lines.append(f"  {DIM}학습 이력 없음{RST}")

    # ── ML Training Service (Fuel + What-if) ────────────────
    ml_alive, fuel_st, whatif_st, fuel_hist, whatif_hist = read_ml_training()
    ml_status_txt = f"{G}ALIVE{RST}" if ml_alive else f"{DIM}미실행{RST}"
    lines.append(f"\n{BOLD}{W}[ ML-TRAINING-SERVICE  포트 8004 ]  서버: {ml_status_txt}{RST}")
    lines.append(f"{DIM}  Fuel XGBoost 자동 반복 (최대 {MAX_ITER_FUEL}회)  +  What-if Analysis (최대 {MAX_ITER_WHATIF}회){RST}\n")

    # Fuel 상태
    lines.append(f"  {BOLD}▸ Fuel XGBoost{RST}")
    if fuel_st:
        f_iter  = int(fuel_st.get("iteration", len(fuel_hist)) or 0)
        f_max   = int(fuel_st.get("max_iterations", MAX_ITER_FUEL) or MAX_ITER_FUEL)
        f_stage = str(fuel_st.get("stage", "") or "")
        f_prog  = int(fuel_st.get("progress", 0) or 0)
        f_err   = fuel_st.get("error")
        f_m     = dict(fuel_st.get("metrics", {}) or {})

        if f_err:
            lines.append(f"    {R}에러: {f_err}{RST}")
        else:
            f_ratio = f_iter / max(f_max, 1)
            lines.append(f"    반복 진행: {bar(f_ratio, 20)}  ({f_iter}/{f_max} iter)")
            if 0 < f_prog < 100:
                lines.append(f"    현재 작업: {bar(f_prog / 100, 20)}  {f_stage}")
            elif "완료" in f_stage or "수렴" in f_stage:
                f_r2   = float(f_m.get("R2", 0) or 0)
                f_rmse = float(f_m.get("RMSE", 0) or 0)
                lines.append(f"    {G}완료{RST}  R²={f_r2:.4f}  RMSE={f_rmse:.6f}")

        if fuel_hist:
            lines.append(f"    {'iter':^5} {'R²':^7} {'RMSE':^12} {'시그널':<22} {'개선':^5}")
            lines.append(f"    {'─'*5} {'─'*7} {'─'*12} {'─'*22} {'─'*5}")
            for rec in _tail(fuel_hist, 3):
                fm   = dict(rec.get("metrics", {}) or {})
                sigs = str(",".join(list(rec.get("signals", []))))[:20]
                imp  = f"{G}↑{RST}" if rec.get("improved") else f"{DIM}─{RST}"
                conv = f" {G}✓{RST}" if rec.get("converged") else ""
                lines.append(
                    f"    {int(rec['iteration']):^5} "
                    f"{float(fm.get('R2', 0) or 0):^7.4f} "
                    f"{float(fm.get('RMSE', 0) or 0):^12.6f} "
                    f"{sigs:<22} {imp}{conv}"
                )
    else:
        lines.append(f"    {DIM}학습 이력 없음{RST}")

    # What-if 상태
    lines.append(f"\n  {BOLD}▸ What-if Analysis{RST}")
    if whatif_st:
        w_iter  = int(whatif_st.get("iteration", len(whatif_hist)) or 0)
        w_max   = int(whatif_st.get("max_iterations", MAX_ITER_WHATIF) or MAX_ITER_WHATIF)
        w_stage = str(whatif_st.get("stage", "") or "")
        w_err   = whatif_st.get("error")
        w_count = int(whatif_st.get("scenarios_count", 0) or 0)
        w_route = str(whatif_st.get("route", "") or "")
        w_ic    = str(whatif_st.get("ice_class", "") or "")

        if w_err:
            lines.append(f"    {R}에러: {w_err}{RST}")
        else:
            w_ratio = w_iter / max(w_max, 1)
            lines.append(f"    반복 진행: {bar(w_ratio, 20)}  ({w_iter}/{w_max} iter)")
            if "완료" in w_stage or "수렴" in w_stage or "달성" in w_stage:
                lines.append(f"    {G}완료{RST}  시나리오 {w_count}개  ({w_route}/{w_ic})")
            elif w_stage and w_stage != "대기 중":
                lines.append(f"    {Y}{w_stage}{RST}  ({w_route}/{w_ic})")

        if whatif_hist:
            lines.append(f"    {'iter':^5} {'시나리오':^6} {'RIO분산':^8} {'시그널':<22} {'개선':^5}")
            lines.append(f"    {'─'*5} {'─'*6} {'─'*8} {'─'*22} {'─'*5}")
            for rec in _tail(whatif_hist, 3):
                wq   = dict(rec.get("quality", {}) or {})
                sigs = str(",".join(list(rec.get("signals", []))))[:20]
                imp  = f"{G}↑{RST}" if rec.get("improved") else f"{DIM}─{RST}"
                conv = f" {G}✓{RST}" if rec.get("converged") else ""
                lines.append(
                    f"    {int(rec['iteration']):^5} "
                    f"{int(wq.get('scenarios_count', 0) or 0):^6} "
                    f"{float(wq.get('avg_rio_spread', 0) or 0):^8.4f} "
                    f"{sigs:<22} {imp}{conv}"
                )
    else:
        lines.append(f"    {DIM}실행 이력 없음{RST}")

    # ── 푸터 ─────────────────────────────────────────────────
    lines.append(f"\n{BOLD}{C}{'═'*72}{RST}")
    lines.append(f"{DIM}  갱신 주기 {REFRESH}초  |  Ctrl+C 로 종료{RST}\n")

    print("".join(lines))

# ── 메인 루프 ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Windows 터미널 UTF-8 강제 설정
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    print("모니터 시작 중...")
    try:
        while True:
            render()
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        print("\n모니터 종료.")
