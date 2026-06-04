"""
land_mask.py
============
frontend/public/data/landMaskGlobal.{meta.json,bin} (Natural Earth 10m
해안선 기반 0.05° 비트팩 전역 육지 마스크) 를 Python 에서 그대로 로드해
voyage 경로의 육지 교차를 제거(동적 해상 우회)한다.

프론트엔드 landMaskGlobal.js 와 **동일한 마스크·동일한 격자 A\\*** 를 사용해
백엔드 사전계산 trace 와 프론트 런타임 회피가 좌표계상 일치하도록 한다.

제공
----
- LandMask.is_land(lat, lon)          : 전 지구 육지 판정
- LandMask.segment_crosses_land(a, b) : 두 점 사이 great-circle 근사 샘플 교차
- LandMask.find_water_detour(a, b)    : 격자 A* 해상 우회 경유점
- refine_route(route, mask)           : 경로 전체를 육지 비교차로 정합
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

from pipeline.icebreaker.models import Position

# backend/pipeline/icebreaker/ -> Digital_twin/
_REPO = Path(__file__).resolve().parents[3]
_DATA_DIR = _REPO / "frontend" / "public" / "data"
_DEG_TO_KM = 111.32


def _wrap_lon(x: float) -> float:
    return ((x + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


def _lon_delta(a: float, b: float) -> float:
    d = b - a
    if d > 180.0:
        d -= 360.0
    if d < -180.0:
        d += 360.0
    return d


class LandMask:
    """0.05° 비트팩 전역 육지 마스크."""

    def __init__(self, meta: dict, packed: bytes):
        self.res: float = meta["res"]
        self.cols: int = meta["cols"]
        self.rows: int = meta["rows"]
        self._packed = packed

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "LandMask":
        d = data_dir or _DATA_DIR
        meta = json.loads((d / "landMaskGlobal.meta.json").read_text(encoding="utf-8"))
        packed = (d / "landMaskGlobal.bin").read_bytes()
        return cls(meta, packed)

    # ── 격자 ↔ 좌표 ──
    def _col_of(self, lon: float) -> int:
        return math.floor((lon + 180.0) / self.res)

    def _row_of(self, lat: float) -> int:
        return math.floor((lat + 90.0) / self.res)

    def _lon_of_col(self, c: int) -> float:
        return -180.0 + (c + 0.5) * self.res

    def _lat_of_row(self, r: int) -> float:
        return -90.0 + (r + 0.5) * self.res

    def _cell_land(self, col: int, row: int) -> bool:
        if row < 0 or row >= self.rows:
            return False
        c = (col % self.cols + self.cols) % self.cols
        idx = row * self.cols + c
        return (self._packed[idx >> 3] >> (idx & 7)) & 1 == 1

    def _cell_blocked(self, col: int, row: int, margin: int = 0) -> bool:
        """margin>0 이면 Chebyshev margin 이내에 육지가 있어도 차단(연안 clearance)."""
        if margin <= 0:
            return self._cell_land(col, row)
        for dr in range(-margin, margin + 1):
            for dc in range(-margin, margin + 1):
                if self._cell_land(col + dc, row + dr):
                    return True
        return False

    def is_land(self, lat: float, lon: float) -> bool:
        if lat < -90.0 or lat > 90.0:
            return False
        return self._cell_land(self._col_of(lon), self._row_of(lat))

    def segment_crosses_land(
        self, a: Position, b: Position, step_km: float = 1.5, margin: int = 0
    ) -> bool:
        """a→b 직선(등각 근사)을 step_km 간격으로 샘플해 육지 교차 여부.

        margin>0 이면 육지에서 margin 셀 이내로 접근해도 교차로 판정(clearance).
        great-circle(선박)와 직선(검사) 보간 차이를 흡수하는 안전 여유.
        """
        mid_lat = (a["lat"] + b["lat"]) * 0.5
        seg_km = math.hypot(
            (b["lat"] - a["lat"]) * _DEG_TO_KM,
            _lon_delta(a["lon"], b["lon"]) * _DEG_TO_KM * math.cos(math.radians(mid_lat)),
        )
        n = max(2, math.ceil(seg_km / step_km))
        d_lon = _lon_delta(a["lon"], b["lon"])
        for i in range(1, n):
            t = i / n
            lat = a["lat"] + (b["lat"] - a["lat"]) * t
            lon = _wrap_lon(a["lon"] + d_lon * t)
            if self._cell_blocked(self._col_of(lon), self._row_of(lat), margin):
                return True
        return False

    def _snap_to_water(self, col: int, row: int) -> tuple[int, int]:
        """육지 셀이면 BFS 로 최근접 해상 셀로 스냅."""
        if not self._cell_land(col, row):
            return col, row
        seen = {row * self.cols + col}
        frontier = [(col, row)]
        for _ring in range(120):
            if not frontier:
                break
            nxt = []
            for cc, rr in frontier:
                for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nc = ((cc + dc) % self.cols + self.cols) % self.cols
                    nr = rr + dr
                    if nr < 0 or nr >= self.rows:
                        continue
                    k = nr * self.cols + nc
                    if k in seen:
                        continue
                    seen.add(k)
                    if not self._cell_land(nc, nr):
                        return nc, nr
                    nxt.append((nc, nr))
            frontier = nxt
        return col, row

    def snap_position_to_water(self, pos: Position) -> Position:
        """좌표가 육지 셀이면 최근접 해상 셀 중심으로 스냅. 바다면 그대로."""
        col = self._col_of(pos["lon"])
        row = self._row_of(pos["lat"])
        if not self._cell_land(col, row):
            return pos
        nc, nr = self._snap_to_water(col, row)
        if self._cell_land(nc, nr):
            return pos  # 스냅 실패(내륙 깊숙) — 원본 유지
        return {
            "lat": round(self._lat_of_row(nr), 3),
            "lon": round(self._lon_of_col(nc), 3),
        }

    def find_water_detour(
        self,
        a: Position,
        b: Position,
        margin_cells: int = 60,
        max_iter: int = 200000,
        clearance: int = 1,
    ) -> list[Position] | None:
        """격자 A* 로 a→b 해상 우회 경유점 산출(시작/끝 제외). 실패 시 None.

        landMaskGlobal.js findWaterDetour 의 Python 포팅 — 날짜변경선 래핑을
        위해 열은 from 기준 '상대 열' 연속 좌표로 탐색한다. clearance>0 이면
        단순화 단계에서 육지에 clearance 셀 이내로 접근하는 직선 병합을 금지해
        해안 여유를 확보(great-circle 보간 오차 흡수).
        """
        sc, sr = self._snap_to_water(self._col_of(a["lon"]), self._row_of(a["lat"]))
        gc, gr = self._snap_to_water(self._col_of(b["lon"]), self._row_of(b["lat"]))

        rel_gc = sc + _lon_delta(self._lon_of_col(sc), self._lon_of_col(gc)) / self.res
        min_r = max(0, min(sr, gr) - margin_cells)
        max_r = min(self.rows - 1, max(sr, gr) + margin_cells)
        min_rc = min(sc, rel_gc) - margin_cells
        max_rc = max(sc, rel_gc) + margin_cells

        def key(c: int, r: int) -> int:
            return r * self.cols + ((c % self.cols) + self.cols) % self.cols

        start_key = key(sc, sr)
        goal_key = key(gc, gr)
        g = {start_key: 0.0}
        came: dict[int, int] = {}
        rel_col = {start_key: float(sc)}
        open_heap: list[tuple[float, int]] = [(0.0, start_key)]
        dirs = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        closed: set[int] = set()
        it = 0

        while open_heap and it < max_iter:
            it += 1
            _f, cur = heapq.heappop(open_heap)
            if cur == goal_key:
                break
            if cur in closed:
                continue
            closed.add(cur)
            cr = cur // self.cols
            cc_col = cur % self.cols
            cc_rel = rel_col[cur]
            cg = g[cur]
            for dc, dr in dirs:
                nr_abs = cr + dr
                if nr_abs < min_r or nr_abs > max_r:
                    continue
                n_rel = cc_rel + dc
                if n_rel < min_rc or n_rel > max_rc:
                    continue
                nc = (round(n_rel) % self.cols + self.cols) % self.cols
                if self._cell_land(nc, nr_abs):
                    continue
                if dc and dr:  # 코너 컷팅 금지
                    orth_a = ((cc_col + dc) % self.cols + self.cols) % self.cols
                    if self._cell_land(orth_a, cr) or self._cell_land(cc_col, nr_abs):
                        continue
                nk = nr_abs * self.cols + nc
                step = 1.4142 if (dc and dr) else 1.0
                ng = cg + step
                if ng < g.get(nk, math.inf):
                    g[nk] = ng
                    came[nk] = cur
                    rel_col[nk] = n_rel
                    h = math.hypot(abs(n_rel - rel_gc), nr_abs - gr)
                    heapq.heappush(open_heap, (ng + h, nk))

        if goal_key not in came and goal_key != start_key:
            return None

        # 경로 복원
        cells = []
        k: int | None = goal_key
        guard = 0
        while k is not None and guard < 100000:
            guard += 1
            cells.append(k)
            k = came.get(k)
        cells.reverse()
        if len(cells) < 2:
            return None

        cell_pts: list[Position] = [
            {"lat": self._lat_of_row(kk // self.cols), "lon": self._lon_of_col(kk % self.cols)}
            for kk in cells
        ]
        pts: list[Position] = [a, *cell_pts, b]

        # line-of-sight 단순화 (clearance 여유를 두고 병합)
        out: list[Position] = [pts[0]]
        anchor = 0
        for i in range(2, len(pts)):
            if self.segment_crosses_land(pts[anchor], pts[i], margin=clearance):
                out.append(pts[i - 1])
                anchor = i - 1
        out.append(pts[-1])

        # 잔여 교차 시 미단순화 전체 경로 사용
        safe = all(
            not self.segment_crosses_land(out[i], out[i + 1], margin=clearance)
            for i in range(len(out) - 1)
        )
        final_pts = out if safe else pts

        return [
            {"lat": round(p["lat"], 3), "lon": round(p["lon"], 3)}
            for p in final_pts[1:-1]
        ]


def refine_route(route: list[Position], mask: LandMask) -> list[Position]:
    """경로의 각 인접 구간이 육지를 가로지르면 해상 우회 경유점을 삽입.

    - 시작/끝(항만)은 작성된 좌표 그대로 유지(선박이 항구에서 출항/입항).
    - 중간 경유점이 육지 셀(0.05° 해상도상 연안 셀) 위면 최근접 해상으로 스냅.
    - 그래도 구간이 육지를 가로지르면 격자 A* 해상 우회 경유점 삽입.

    반환 경로의 항만-인접 구간(불가피)을 제외한 모든 인접 구간은 육지를
    가로지르지 않는다(우회 실패 구간은 원본 유지 — 호출측이 검증/로깅).
    """
    if not route:
        return route
    last_i = len(route) - 1
    refined: list[Position] = [route[0]]
    for i in range(1, len(route)):
        b = route[i]
        # 중간 경유점이 연안 육지 셀이면 해상으로 스냅(항만 시종점은 보존)
        if i != last_i:
            b = mask.snap_position_to_water(b)
        a = refined[-1]
        # margin=1: 해안에 1셀 이내로 스치는 구간도 우회(great-circle 보간 여유)
        if mask.segment_crosses_land(a, b, margin=1):
            detour = mask.find_water_detour(a, b, clearance=1)
            if detour:
                refined.extend(detour)
        refined.append(b)
    return refined
