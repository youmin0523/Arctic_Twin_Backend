import http.client
import json


def check_health(port, endpoint):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", endpoint)
        resp = conn.getresponse()
        data = resp.read().decode()
        print(f"Port {port} ({endpoint}): Status {resp.status}, Body: {data}")
        return resp.status == 200
    except Exception as e:
        print(f"Port {port} ({endpoint}): Failed - {e}")
        return False


print("--- 서비스 상태 체크 ---")
check_health(8001, "/api/rl/health")
check_health(8002, "/api/report/health")
