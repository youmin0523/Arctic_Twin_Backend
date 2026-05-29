"""
status_watch.py — 3분마다 자동 상태 보고 (터미널 출력)
실행: python status_watch.py
종료: Ctrl+C
"""
import json
import time
from datetime import datetime
from pathlib import Path
from urllib import request as ur

INTERVAL = 180  # 3분
BASE = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트

def query(url):
    try:
        with ur.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except:
        return None

def read_history(path, key):
    p = Path(path)
    if not p.exists():
        return 0, 0.0, 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        its = d if isinstance(d, list) else d.get("iterations", [])
        best = max((it.get("metrics", {}).get(key) or 0 for it in its), default=0)
        conv = sum(1 for it in its if it.get("converged"))
        return len(its), best, conv
    except:
        return 0, 0.0, 0

def read_wi():
    p = BASE / "backend/services/report-service" / "data" / "whatif_iterative_history.json"
    if not p.exists():
        return 0, 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        its = d if isinstance(d, list) else d.get("iterations", [])
        best = max((it.get("quality", {}).get("scenarios_count") or 0 for it in its), default=0)
        return len(its), best
    except:
        return 0, 0

def read_rl_files():
    files = list((BASE / "backend/services/rl-pipeline" / "models").glob("iterative_history_*.json"))
    ti = 0; cv = 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            its = d if isinstance(d, list) else d.get("iterations", [])
            ti += len(its)
            if its and its[-1].get("converged"): cv += 1
        except: pass
    return len(files), ti, cv

def get_cpu():
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        return f"CPU:{cpu:.1f}% RAM여유:{mem.available/1024**3:.1f}GB"
    except:
        return "리소스 정보 없음"

def read_ct_log():
    p = BASE / "logs" / "continuous_trainer.log"
    if not p.exists():
        return "로그 없음"
    try:
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        return lines[-1].split("] ", 1)[-1] if lines else "없음"
    except:
        return "읽기 실패"

def report():
    now = datetime.now().strftime("%H:%M:%S")

    # API 조회
    r1 = query("http://127.0.0.1:8001/api/rl/multi/status")
    r2 = query("http://127.0.0.1:8002/api/report/rl/multi/status")
    r3 = query("http://127.0.0.1:8003/api/sar/status")
    r4 = query("http://127.0.0.1:8004/api/ml/status")

    # 파일 히스토리
    rl_files, rl_iters, rl_conv = read_rl_files()
    rp_files2 = list((BASE / "backend/services/report-service" / "data").glob("departure_iterative_history_*.json"))
    sar_iters, sar_best, _ = read_history(str(BASE / "backend" / "pipeline" / "models" / "iceberg_iterative_history.json"), "mAP50")
    fuel_iters, fuel_best, _ = read_history(str(BASE / "backend/services/ml-pipeline" / "models" / "fuel_iterative_history.json"), "R2")
    wi_iters, wi_best = read_wi()
    ct_log = read_ct_log()

    print("\n" + "=" * 60)
    print(f"  ⏱  {now}  3분 정기 보고")
    print("=" * 60)

    # RL
    if r1:
        s1 = "ALIVE" if r1 else "미응답"
        running = r1.get("running_models", 0)
        print(f"\n🤖 RL-PIPELINE (8001)")
        print(f"  • 상태: {s1}")
        print(f"  • 전체: {rl_iters}/1260 iter 완료, 수렴 {rl_conv}개")
        print(f"  • 실행중: {running}개 모델")
    else:
        print(f"\n🤖 RL-PIPELINE (8001)")
        print(f"  • 상태: 미응답")
        print(f"  • 전체(파일): {rl_iters}/1260 iter, 수렴 {rl_conv}개")

    # Report
    if r2:
        print(f"\n🚢 REPORT-SERVICE (8002)")
        print(f"  • 상태: ALIVE")
        print(f"  • 전체: {len(rp_files2)*0}/420 iter 완료, 수렴 0개")
    else:
        print(f"\n🚢 REPORT-SERVICE (8002)")
        print(f"  • 상태: 미응답")

    # SAR
    print(f"\n🛰  SAR YOLOv8 (8003)")
    if r3 and "is_training" in r3:
        print(f"  • 상태: ALIVE")
        print(f"  • 반복: {r3.get('iteration',0)}/{r3.get('max_iterations',3)}회, 최고 mAP50={r3.get('metrics',{}).get('mAP50') or 0:.4f}")
        print(f"  • 현재: {r3.get('stage','?')}")
    else:
        print(f"  • 상태: 미응답/포트충돌")
        print(f"  • 반복(파일): {sar_iters}/3회, 최고 mAP50={sar_best:.4f}")

    # Fuel
    print(f"\n⛽ FUEL XGBoost")
    if r4 and "fuel" in r4:
        fs = r4["fuel"]
        print(f"  • 반복: {fs.get('iteration',0)}/5회, 최고 R²={fs.get('best_r2',0):.4f}")
        print(f"  • 현재: {fs.get('stage','?')}")
    else:
        print(f"  • 반복(파일): {fuel_iters}/5회, 최고 R²={fuel_best:.4f}")
        print(f"  • 현재: {ct_log[:80]}")

    # What-if
    print(f"\n🗺  WHAT-IF ANALYSIS")
    if r4 and "whatif" in r4:
        ws = r4["whatif"]
        print(f"  • 반복: {ws.get('iteration',0)}/3회, 시나리오 {ws.get('best_scenarios',0)}개")
        print(f"  • 현재: {ws.get('stage','?')}")
    else:
        print(f"  • 반복(파일): {wi_iters}/3회, 시나리오 {wi_best}개")

    # 시스템
    print(f"\n📊 시스템: {get_cpu()}")
    print("=" * 60)


if __name__ == "__main__":
    print("Digital Twin 상태 모니터 시작 (3분 간격, Ctrl+C 종료)")
    while True:
        try:
            report()
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\n모니터 종료.")
            break
        except Exception as e:
            print(f"[오류] {e}")
            time.sleep(INTERVAL)
