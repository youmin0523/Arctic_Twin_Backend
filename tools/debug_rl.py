import http.client
import json
import time

def call_api(port, method, endpoint, payload=None):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-type": "application/json"} if payload else {}
        body = json.dumps(payload) if payload else None
        conn.request(method, endpoint, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        return resp.status, data
    except Exception as e:
        return None, str(e)

print("--- 서비스 진단 및 트리거 ---")

services = [
    {"name": "RL-C (Avoidance)", "port": 8001, "health": "/api/rl/health", "train": "/api/rl/train", "status": "/api/rl/status"},
    {"name": "RL-A (Report)", "port": 8002, "health": "/api/report/health", "train": "/api/report/rl/train", "status": "/api/report/rl/train-status/debug"}
]

for s in services:
    print(f"\n[{s['name']}] 포트 {s['port']} 체크 중...")
    status, body = call_api(s['port'], "GET", s['health'])
    if status == 200:
        print(f"  - Health OK: {body}")
        # 상태 확인
        st_code, st_body = call_api(s['port'], "GET", s['status'])
        print(f"  - Current Status: {st_body}")
        
        # 학습 안하고 있으면 트리거
        try:
            st_data = json.loads(st_body)
            # rl-pipeline은 is_training 필드 사용
            is_training = st_data.get("is_training", False)
            if not is_training:
                print("  - 학습 중 아님. 트리거 시작...")
                t_code, t_body = call_api(s['port'], "POST", s['train'], {"curriculum": True})
                print(f"  - Trigger Result: {t_code} {t_body}")
            else:
                print("  - 이미 학습 중입니다.")
        except:
            print("  - 상태 해석 실패. 강제 트리거 시도...")
            t_code, t_body = call_api(s['port'], "POST", s['train'], {"curriculum": True})
            print(f"  - Trigger Result: {t_code} {t_body}")
    else:
        print(f"  - Health Fail: {body}")

# Backend (8000)
print("\n[Backend] 포트 8000 체크 중...")
status, body = call_api(8000, "GET", "/health")
print(f"  - Status {status}: {body}")
