"""
routes_loader.py
================
프론트엔드 arcticRoutes.js 파일을 정규식으로 파싱해 Python dict로 로드.

외부 의존성 없음(표준 lib만). JS 파일이 단순 ES6 export 형태이므로
정규식 파싱으로 충분. 유지보수 이중화를 피하기 위해 런타임 파싱 선택.
"""

from __future__ import annotations
import re
from pathlib import Path

from pipeline.icebreaker.models import Position


# arcticRoutes.js 기본 경로 — Digital_twin/frontend/src/data/arcticRoutes.js
_THIS = Path(__file__).resolve()
# icebreaker/ -> pipeline/ -> backend/ -> Digital_twin/
DEFAULT_ARCTIC_ROUTES_JS = (
    _THIS.parents[3] / "frontend" / "src" / "data" / "arcticRoutes.js"
)

_ROUTE_NAMES = ("NSR", "NWP", "TSR", "SUEZ", "CAPE", "ROSS", "PENINSULA")

# 먼저 `export const ROUTES = { ... };` 블록 전체 추출
_ROUTES_EXPORT_RE = re.compile(
    r"export\s+const\s+ROUTES\s*=\s*\{(.*?)\n\}\s*;", re.DOTALL
)
# 각 경로: NAME: [ ... ], (다음 NAME 또는 블록 끝)
_ROUTE_BLOCK_RE = re.compile(
    r"(NSR|NWP|TSR|SUEZ|CAPE|ROSS|PENINSULA)\s*:\s*\[(.*?)\]\s*,?\s*"
    r"(?=(?:NSR|NWP|TSR|SUEZ|CAPE|ROSS|PENINSULA)\s*:|$)",
    re.DOTALL,
)
# 웨이포인트: { lon: X, lat: Y, ... }
_WAYPOINT_RE = re.compile(
    r"\{\s*lon\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*lat\s*:\s*(-?\d+(?:\.\d+)?)",
)


def load_routes(js_path: Path | None = None) -> dict[str, list[Position]]:
    """arcticRoutes.js를 파싱해 {route_name: [Position, ...]} 반환.

    반환 형식은 dispatcher 의 Position TypedDict 와 동일하므로
    그대로 dispatch_tick/forward_route 에 투입 가능.
    """
    path = js_path or DEFAULT_ARCTIC_ROUTES_JS
    if not path.exists():
        raise FileNotFoundError(f"arcticRoutes.js not found at: {path}")

    content = path.read_text(encoding="utf-8")

    export_match = _ROUTES_EXPORT_RE.search(content)
    if not export_match:
        raise ValueError("Could not locate `export const ROUTES = {...};` block")
    routes_body = export_match.group(1)

    # 경로 키 위치 수집 후 각 키 사이 블록을 순차적으로 슬라이스
    key_positions: list[tuple[int, str]] = []
    for km in re.finditer(r"(NSR|NWP|TSR|SUEZ|CAPE|ROSS|PENINSULA)\s*:\s*\[", routes_body):
        key_positions.append((km.start(), km.group(1)))
    key_positions.sort()

    routes: dict[str, list[Position]] = {}
    for i, (pos, name) in enumerate(key_positions):
        end = key_positions[i + 1][0] if i + 1 < len(key_positions) else len(routes_body)
        block = routes_body[pos:end]
        waypoints: list[Position] = []
        for wm in _WAYPOINT_RE.finditer(block):
            lon = float(wm.group(1))
            lat = float(wm.group(2))
            waypoints.append({"lat": lat, "lon": lon})
        if waypoints:
            routes[name] = waypoints

    missing = [n for n in _ROUTE_NAMES if n not in routes]
    if missing:
        raise ValueError(
            f"arcticRoutes.js parsing incomplete, missing routes: {missing}"
        )
    return routes


if __name__ == "__main__":
    rs = load_routes()
    for name in _ROUTE_NAMES:
        print(f"{name:5s}: {len(rs[name])} waypoints, "
              f"first={rs[name][0]}, last={rs[name][-1]}")
