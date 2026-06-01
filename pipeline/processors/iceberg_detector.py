#!/usr/bin/env python3
"""
iceberg_detector.py
===================
YOLOv8 기반 SAR 빙산 탐지 추론 파이프라인.

Sentinel-1 GRD SAR 제품에서 빙산을 자동 탐지하고
realBergData_latest.json을 업데이트합니다.

사용법:
  python iceberg_detector.py --latest              # 최신 SAR 제품에서 탐지
  python iceberg_detector.py --product <name.zip>   # 특정 제품 처리
  python iceberg_detector.py --max-products 5       # 최대 N개 제품 처리
  python iceberg_detector.py --confidence 0.5       # 신뢰도 임계값

출력:
  data/realBergData_latest.json (병합 업데이트)
  data/sar_detections_latest.json (SAR 탐지 전용 결과)
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger("iceberg_detector")

PIPELINE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PIPELINE_DIR.parent / "data"
MODELS_DIR = PIPELINE_DIR / "models"
MODEL_PATH = MODELS_DIR / "iceberg_yolov8.pt"
CATALOG_FILE = DATA_DIR / "sentinel1_catalog_latest.json"
BERG_FILE = DATA_DIR / "realBergData_latest.json"
SAR_DETECTION_FILE = DATA_DIR / "sar_detections_latest.json"

# 탐지 설정
DEFAULT_CONFIDENCE = 0.4
NMS_IOU_THRESHOLD = 0.45
MERGE_DISTANCE_KM = 5.0  # 기존 빙산과 이 거리 내이면 중복으로 간주


class IcebergDetector:
    """YOLOv8 기반 SAR 빙산 탐지기."""

    def __init__(self, model_path: Path = MODEL_PATH, confidence: float = DEFAULT_CONFIDENCE):
        self.model_path = model_path
        self.confidence = confidence
        self.model = None

    def load_model(self):
        """YOLOv8 모델을 로드합니다."""
        if not self.model_path.exists():
            log.error("모델 파일을 찾을 수 없습니다: %s", self.model_path)
            log.error("먼저 iceberg_model_trainer.py로 모델을 학습하세요.")
            raise FileNotFoundError(f"모델 없음: {self.model_path}")

        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError("ultralytics가 필요합니다: pip install ultralytics")

        log.info("YOLOv8 모델 로드: %s", self.model_path)
        self.model = YOLO(str(self.model_path))

    def detect_tiles(self, tiles: list[dict]) -> list[dict]:
        """
        전처리된 타일 목록에서 빙산을 탐지합니다.

        Args:
            tiles: [{"tile": ndarray(3,640,640), "geo_offset": {...}}, ...]

        Returns:
            탐지 결과: [{"bbox_pixel": [x1,y1,x2,y2], "confidence": float,
                        "geo_offset": dict, "class": int}, ...]
        """
        if not self.model:
            self.load_model()

        all_detections = []

        for i, tile_data in enumerate(tiles):
            tile = tile_data["tile"]  # (3, 640, 640)
            geo = tile_data.get("geo_offset", {})

            # YOLO 입력: (H, W, C) uint8 또는 float
            # 3채널 float32 → uint8 변환
            tile_hwc = np.transpose(tile, (1, 2, 0))  # (640, 640, 3)
            tile_uint8 = (tile_hwc * 255).astype(np.uint8)

            # 추론
            assert self.model is not None
            results = self.model.predict(
                tile_uint8,
                conf=self.confidence,
                iou=NMS_IOU_THRESHOLD,
                verbose=False,
            )

            # 결과 파싱
            for result in results:
                if result.boxes is None or len(result.boxes) == 0:
                    continue

                for box in result.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    conf = float(box.conf[0].cpu())
                    cls = int(box.cls[0].cpu())

                    all_detections.append({
                        "tile_index": i,
                        "bbox_pixel": [float(x1), float(y1), float(x2), float(y2)],
                        "confidence": round(conf, 4),
                        "class": cls,
                        "geo_offset": geo,
                    })

            if (i + 1) % 50 == 0:
                log.info("탐지 진행: %d/%d 타일, 누적 %d 빙산", i + 1, len(tiles), len(all_detections))

        log.info("탐지 완료: %d 타일 → %d 빙산", len(tiles), len(all_detections))
        return all_detections


class DetectionPostprocessor:
    """탐지 결과 후처리: 좌표변환, 크기추정, NMS, JSON 병합."""

    PIXEL_RESOLUTION_M = 25.0  # Sentinel-1 GRD 해상도 (약 25m)

    @staticmethod
    def pixel_to_latlon(bbox: list[float], geo_offset: dict) -> dict:
        """
        타일 내 픽셀 좌표를 위경도로 변환합니다.

        Args:
            bbox: [x1, y1, x2, y2] 픽셀 좌표
            geo_offset: {"origin_lon", "origin_lat", "pixel_size_x", "pixel_size_y"}

        Returns:
            {"lon": float, "lat": float, "lon_min": float, "lat_min": float, ...}
        """
        if not geo_offset:
            return {"lon": 0.0, "lat": 0.0}

        x1, y1, x2, y2 = bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

        origin_lon = geo_offset.get("origin_lon", 0)
        origin_lat = geo_offset.get("origin_lat", 0)
        px = geo_offset.get("pixel_size_x", 0.001)
        py = geo_offset.get("pixel_size_y", -0.001)

        lon = origin_lon + cx * px
        lat = origin_lat + cy * py

        return {
            "lon": round(lon, 5),
            "lat": round(lat, 5),
        }

    @classmethod
    def estimate_size(cls, bbox: list[float]) -> dict:
        """바운딩 박스에서 빙산 물리 크기를 추정합니다."""
        x1, y1, x2, y2 = bbox
        width_px = abs(x2 - x1)
        height_px = abs(y2 - y1)

        width_m = width_px * cls.PIXEL_RESOLUTION_M
        height_m = height_px * cls.PIXEL_RESOLUTION_M

        return {
            "length_m": round(max(width_m, height_m), 1),
            "width_m": round(min(width_m, height_m), 1),
        }

    @staticmethod
    def classify_by_size(length_m: float) -> str:
        """크기 기반 빙산 분류."""
        if length_m < 50:
            return "growler"
        elif length_m < 200:
            return "small"
        elif length_m < 500:
            return "medium"
        elif length_m < 2000:
            return "large"
        else:
            return "tabular"

    @staticmethod
    def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        """두 좌표 사이 거리 (km)."""
        from math import radians, sin, cos, sqrt, atan2
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    def cross_tile_nms(self, detections: list[dict], iou_threshold: float = 0.3) -> list[dict]:
        """
        크로스타일 NMS: 오버랩 영역의 중복 탐지를 제거합니다.
        좌표 기반 거리로 중복 판단 (픽셀 NMS 대신 지리 좌표 NMS).
        """
        if not detections:
            return []

        # 신뢰도 기준 내림차순 정렬
        sorted_dets = sorted(detections, key=lambda d: d["confidence"], reverse=True)
        keep = []

        for det in sorted_dets:
            pos = det.get("position", {})
            is_duplicate = False

            for kept in keep:
                kept_pos = kept.get("position", {})
                dist = self.haversine_km(
                    pos.get("lon", 0), pos.get("lat", 0),
                    kept_pos.get("lon", 0), kept_pos.get("lat", 0),
                )
                # 1km 이내이면 중복
                if dist < 1.0:
                    is_duplicate = True
                    break

            if not is_duplicate:
                keep.append(det)

        log.info("크로스타일 NMS: %d → %d (제거: %d)",
                 len(detections), len(keep), len(detections) - len(keep))
        return keep

    def process(self, raw_detections: list[dict], product_name: str = "") -> list[dict]:
        """
        원시 탐지 결과를 후처리하여 최종 빙산 목록을 생성합니다.

        Args:
            raw_detections: detect_tiles()의 출력
            product_name: SAR 제품 이름 (추적용)

        Returns:
            최종 빙산 목록 (realBergData 호환 포맷)
        """
        processed = []

        for i, det in enumerate(raw_detections):
            position = self.pixel_to_latlon(det["bbox_pixel"], det["geo_offset"])
            size = self.estimate_size(det["bbox_pixel"])
            berg_type = self.classify_by_size(size["length_m"])

            processed.append({
                "id": f"SAR_{product_name[:20]}_{i:03d}",
                "lon": position["lon"],
                "lat": position["lat"],
                "length_m": size["length_m"],
                "width_m": size["width_m"],
                "type": berg_type,
                "last_update": datetime.now().strftime("%m/%d/%Y"),
                "source": "sentinel1_sar",
                "confidence": det["confidence"],
                "product_name": product_name,
                "position": position,
            })

        # 크로스타일 NMS
        final = self.cross_tile_nms(processed)
        return final

    def merge_with_existing(
        self,
        new_bergs: list[dict],
        existing_file: Path = BERG_FILE,
        merge_distance_km: float = MERGE_DISTANCE_KM,
    ) -> list[dict]:
        """
        SAR 탐지 결과를 기존 realBergData_latest.json과 병합합니다.

        - 기존 빙산과 merge_distance_km 이내이면 중복 → 건너뜀
        - 새 빙산은 source="sentinel1_sar"로 추가
        """
        existing = []
        if existing_file.exists():
            with open(existing_file, encoding="utf-8") as f:
                raw = json.load(f)
                existing = raw if isinstance(raw, list) else raw.get("bergs", raw.get("icebergs", []))

        added = 0
        for new in new_bergs:
            is_duplicate = False
            for old in existing:
                dist = self.haversine_km(
                    new["lon"], new["lat"],
                    old.get("lon", 0), old.get("lat", 0),
                )
                if dist < merge_distance_km:
                    is_duplicate = True
                    break

            if not is_duplicate:
                # position 필드 제거 (내부용)
                berg = {k: v for k, v in new.items() if k != "position"}
                existing.append(berg)
                added += 1

        log.info("병합: 기존 %d + 신규 %d = 총 %d (중복 제거: %d)",
                 len(existing) - added, added, len(existing), len(new_bergs) - added)
        return existing


def run_detection_pipeline(
    max_products: int = 5,
    confidence: float = DEFAULT_CONFIDENCE,
    dry_run: bool = False,
):
    """
    전체 SAR 빙산 탐지 파이프라인을 실행합니다.

    1. Sentinel-1 카탈로그에서 다운로드된 제품 확인
    2. 각 제품을 전처리 → 타일링
    3. YOLOv8으로 빙산 탐지
    4. 후처리 + 기존 데이터 병합
    5. realBergData_latest.json 업데이트
    """
    from .sar_preprocessor import preprocess_product

    # 카탈로그 로드
    if not CATALOG_FILE.exists():
        log.error("Sentinel-1 카탈로그 없음: %s", CATALOG_FILE)
        return

    with open(CATALOG_FILE, encoding="utf-8") as f:
        catalog = json.load(f)

    products = catalog if isinstance(catalog, list) else catalog.get("products", [])

    # 다운로드된 제품만 필터 (file_path가 있는 것)
    downloaded = [p for p in products if p.get("file_path")]

    if not downloaded:
        log.warning("다운로드된 SAR 제품이 없습니다. sentinel1_iw_fetcher.py를 먼저 실행하세요.")
        log.info("카탈로그에 %d개 제품이 색인되어 있지만 파일이 없습니다.", len(products))
        return

    # 최신 제품부터 처리
    downloaded.sort(key=lambda p: p.get("sensing_start", ""), reverse=True)
    to_process = downloaded[:max_products]

    log.info("처리 대상: %d / %d 제품", len(to_process), len(downloaded))

    if dry_run:
        for p in to_process:
            log.info("[DRY RUN] %s (%s)", p.get("name", "?"), p.get("aoi", "?"))
        return

    # 탐지기 초기화
    detector = IcebergDetector(confidence=confidence)
    postprocessor = DetectionPostprocessor()

    all_new_bergs = []

    for i, product in enumerate(to_process):
        product_name = product.get("name", f"product_{i}")
        zip_path = Path(product["file_path"])

        if not zip_path.exists():
            log.warning("파일 없음: %s", zip_path)
            continue

        log.info("[%d/%d] 처리: %s", i + 1, len(to_process), product_name)

        try:
            # 전처리
            work_dir = DATA_DIR / "sar_processed"
            tiles = preprocess_product(zip_path, work_dir)

            if not tiles:
                log.warning("타일 없음 (건너뜀): %s", product_name)
                continue

            # 탐지
            raw_dets = detector.detect_tiles(tiles)

            # 후처리
            bergs = postprocessor.process(raw_dets, product_name)
            all_new_bergs.extend(bergs)

            log.info("  → %d 빙산 탐지", len(bergs))

        except Exception as e:
            log.error("처리 실패 (%s): %s", product_name, e)
            continue

    if not all_new_bergs:
        log.info("새로 탐지된 빙산이 없습니다.")
        return

    # 기존 데이터와 병합
    merged = postprocessor.merge_with_existing(all_new_bergs)

    # 저장
    with open(BERG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    log.info("realBergData_latest.json 업데이트: %d 빙산", len(merged))

    # SAR 전용 탐지 결과도 별도 저장
    sar_result = {
        "detection_time": datetime.now().isoformat(),
        "products_processed": len(to_process),
        "total_detected": len(all_new_bergs),
        "confidence_threshold": confidence,
        "detections": all_new_bergs,
    }
    with open(SAR_DETECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(sar_result, f, indent=2, ensure_ascii=False)
    log.info("SAR 탐지 결과 저장: %s", SAR_DETECTION_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="SAR 빙산 탐지 파이프라인")
    parser.add_argument("--latest", action="store_true", help="최신 제품에서 탐지")
    parser.add_argument("--max-products", type=int, default=5, help="최대 처리 제품 수")
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE, help="신뢰도 임계값")
    parser.add_argument("--dry-run", action="store_true", help="실제 처리 없이 확인만")
    args = parser.parse_args()

    run_detection_pipeline(
        max_products=args.max_products,
        confidence=args.confidence,
        dry_run=args.dry_run,
    )
