"""
asyncio.create_task(asyncio.to_thread(...)) 패턴이 starlette BackgroundTask
대신 동작하는지 빠른 검증.
"""
import asyncio
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import uvicorn

state = {"counter": 0, "done": False}


def slow_work():
    time.sleep(1.0)
    state["counter"] += 1
    state["done"] = True


app = FastAPI()


@app.post("/go")
async def go():
    # 핵심: BackgroundTasks 안 쓰고 직접 schedule
    asyncio.create_task(asyncio.to_thread(slow_work))
    return {"scheduled": True}


@app.get("/status")
async def status():
    return state


if __name__ == "__main__":
    # 별도 thread에서 서버 띄우고 호출 테스트
    import threading
    import httpx

    server_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=8765, log_level="error"),
        daemon=True,
    )
    server_thread.start()
    time.sleep(2)

    with httpx.Client() as c:
        print("POST /go:", c.post("http://127.0.0.1:8765/go").json())
        time.sleep(2)
        print("GET /status:", c.get("http://127.0.0.1:8765/status").json())

    print("counter =", state["counter"], "done =", state["done"])
