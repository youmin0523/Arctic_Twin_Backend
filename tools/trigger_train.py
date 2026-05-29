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
        return resp.status in [200, 202]
    except Exception as e:
        print(f"Port {port} ({endpoint}): Failed - {e}")
        return False

print("--- 강화학습 API 트리거 ---")
# RL-C: Collision Avoidance (Curriculum)
trigger_train(8001, "/api/rl/train", {"curriculum": True})
# RL-A: Departure Scheduling
trigger_train(8002, "/api/report/rl/train", {"curriculum": True})
