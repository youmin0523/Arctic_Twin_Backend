import http.client
import json

def trigger_train(port, endpoint, payload):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {'Content-type': 'application/json'}
        conn.request("POST", endpoint, body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        print(f"Port {port} ({endpoint}): Status {resp.status}, Body: {data}")
        return resp.status in [200, 202, 201]
    except Exception as e:
        print(f"Port {port} ({endpoint}): Failed - {e}")
        return False

print("--- 백엔드 프록시(8000)를 통한 강화학습 API 트리거 ---")
# RL-C: Collision Avoidance
trigger_train(8000, "/api/rl/train", {"curriculum": True, "difficulty": "medium", "timesteps": 500000})

# RL-A: Trend Report (Departure Scheduling)
trigger_train(8000, "/api/report/rl/train", {"curriculum": True})
