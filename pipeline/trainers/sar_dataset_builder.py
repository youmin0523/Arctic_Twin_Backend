#!/usr/bin/env python3
"""
sar_dataset_builder.py
======================
SAR 빙산 탐지 학습용 데이터셋을 구축합니다.

두 가지 소스를 결합합니다:
  1. 자체 데이터: realBergData_latest.json의 빙산 좌표 + Sentinel-1 카탈로그 교차
  2. 공개 데이터: AI4Arctic Sea Ice Challenge 데이터셋 (선택)

출력: YOLO 포맷 디렉토리 구조
  datasets/
    sar_icebergs/
      images/train/    .npy 타일 (3채널, 640×640)
      images/val/
      labels/train/    .txt (YOLO 포맷: class x_center y_center w h)
      labels/val/
      data.yaml        모델 설정 파일
"""

import json
import logging
import random
import shutil
from pathlib import Path

import numpy as np

log = logging.getLogger("sar_dataset_builder")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DATASET_DIR = DATA_DIR / "datasets" / "sar_icebergs"

# 빙산 클래스 정의
CLASSES = ["iceberg"]

# 분할 비율
TRAIN_RATIO = 0.8


def load_berg_ground_truth() -> list[dict]:
    """realBergData_latest.json에서 빙산 좌표 + 크기 로드."""
    berg_file = DATA_DIR / "realBergData_latest.json"
    if not berg_file.exists():
        log.warning("빙산 데이터 파일 없음: %s", berg_file)
        return []

    with open(berg_file, encoding="utf-8") as f:
        raw = json.load(f)

    bergs = raw if isinstance(raw, list) else raw.get("bergs", raw.get("icebergs", []))

    # Arctic 빙산만 필터 (lat > 60)
    arctic = [b for b in bergs if b.get("lat", 0) > 60]
    log.info("빙산 Ground Truth: 전체 %d, 북극(>60°N) %d", len(bergs), len(arctic))
    return arctic


def load_sentinel1_catalog() -> list[dict]:
    """Sentinel-1 카탈로그에서 제품 메타데이터 로드."""
    catalog_file = DATA_DIR / "sentinel1_catalog_latest.json"
    if not catalog_file.exists():
        log.warning("Sentinel-1 카탈로그 없음: %s", catalog_file)
        return []

    with open(catalog_file, encoding="utf-8") as f:
        data = json.load(f)

    products = data if isinstance(data, list) else data.get("products", [])
    log.info("Sentinel-1 카탈로그: %d 제품", len(products))
    return products


def match_bergs_to_footprints(bergs: list[dict], products: list[dict]) -> list[dict]:
    """
    빙산 좌표와 Sentinel-1 제품 촬영 범위를 교차하여 학습 레이블을 생성합니다.

    각 빙산이 어떤 SAR 제품의 촬영 범위 내에 있는지 확인하고,
    해당 제품의 타일에서의 상대 좌표를 계산합니다.
    """
    matched = []

    for product in products:
        aoi = product.get("aoi", "")
        # AOI의 bbox 정보가 있으면 사용
        bbox = _get_aoi_bbox(aoi)
        if not bbox:
            continue

        min_lon, min_lat, max_lon, max_lat = bbox

        for berg in bergs:
            blon = berg.get("lon", 0)
            blat = berg.get("lat", 0)

            if min_lon <= blon <= max_lon and min_lat <= blat <= max_lat:
                matched.append({
                    "berg": berg,
                    "product": product.get("name", ""),
                    "aoi": aoi,
                    "rel_x": (blon - min_lon) / (max_lon - min_lon),
                    "rel_y": 1.0 - (blat - min_lat) / (max_lat - min_lat),  # y축 반전
                    "size_m": berg.get("length_m", 100),
                })

    log.info("빙산-SAR 매칭: %d 쌍", len(matched))
    return matched


def _get_aoi_bbox(aoi_name: str) -> tuple | None:
    """AOI 이름에서 bbox 반환."""
    aois = {
        "svalbard": (10, 76, 35, 81),
        "greenland_east": (-45, 65, -15, 80),
        "jakobshavn": (-55, 68, -45, 72),
        "novaya_zemlya": (48, 70, 62, 77),
    }
    return aois.get(aoi_name)


def generate_synthetic_labels(
    tile_count: int,
    bergs_per_tile_range: tuple[int, int] = (0, 5),
) -> list[dict]:
    """
    실제 SAR 이미지가 없을 때 합성 학습 레이블을 생성합니다.

    SAR에서 빙산은 일반적으로:
    - 밝은 점 (강한 후방산란)
    - 크기: 10~500 픽셀 (25m 해상도 기준 250m~12.5km)
    - 바다 배경(어두움) 위에 고립되어 있음

    이 함수는 합성 타일과 대응하는 YOLO 레이블을 생성합니다.
    """
    labels = []

    for i in range(tile_count):
        n_bergs = random.randint(*bergs_per_tile_range)
        tile_labels = []

        for _ in range(n_bergs):
            # 빙산 크기: 작음(10~30px), 중간(30~80px), 큰(80~200px)
            size_class = random.choices(
                ["small", "medium", "large"],
                weights=[0.5, 0.35, 0.15],
                k=1,
            )[0]

            if size_class == "small":
                w = random.uniform(10, 30) / 640
                h = random.uniform(10, 25) / 640
            elif size_class == "medium":
                w = random.uniform(30, 80) / 640
                h = random.uniform(25, 70) / 640
            else:
                w = random.uniform(80, 200) / 640
                h = random.uniform(70, 180) / 640

            # 가장자리 회피 (빙산이 타일 경계에 걸리지 않도록)
            cx = random.uniform(w / 2 + 0.02, 1.0 - w / 2 - 0.02)
            cy = random.uniform(h / 2 + 0.02, 1.0 - h / 2 - 0.02)

            tile_labels.append({
                "class": 0,  # iceberg
                "x_center": round(cx, 6),
                "y_center": round(cy, 6),
                "width": round(w, 6),
                "height": round(h, 6),
            })

        labels.append({"tile_index": i, "objects": tile_labels})

    log.info("합성 레이블 생성: %d 타일, 평균 %.1f 빙산/타일",
             tile_count, sum(len(l["objects"]) for l in labels) / max(tile_count, 1))
    return labels


def generate_synthetic_tiles(count: int) -> list[np.ndarray]:
    """
    합성 SAR 타일을 생성합니다 (학습 초기 검증용).

    실제 SAR 특성을 모사:
    - 배경: 해수면 σ0 ~ -20~-15 dB (정규화 후 0.15~0.35)
    - 빙산: σ0 ~ -5~+5 dB (정규화 후 0.6~0.9)
    - 스펙클 노이즈: 곱셈 노이즈 (Rayleigh 분포)
    """
    tiles = []

    for _ in range(count):
        # 해수면 배경 (3채널)
        vv_bg = np.random.uniform(0.15, 0.35, (640, 640)).astype(np.float32)
        vh_bg = np.random.uniform(0.10, 0.25, (640, 640)).astype(np.float32)

        # 스펙클 노이즈 추가
        speckle = np.random.rayleigh(scale=0.08, size=(640, 640)).astype(np.float32)
        vv_bg += speckle
        vh_bg += speckle * 0.7

        # VV/VH 비율
        ratio = np.clip(vv_bg / (vh_bg + 1e-6) / 20.0, 0, 1)

        tile = np.stack([
            np.clip(vv_bg, 0, 1),
            np.clip(vh_bg, 0, 1),
            ratio,
        ], axis=0)

        tiles.append(tile)

    return tiles


def inject_synthetic_icebergs(
    tile: np.ndarray,
    labels: list[dict],
) -> np.ndarray:
    """합성 타일에 빙산 시그니처를 주입합니다."""
    tile = tile.copy()
    h, w = tile.shape[1], tile.shape[2]

    for lbl in labels:
        cx, cy = int(lbl["x_center"] * w), int(lbl["y_center"] * h)
        bw, bh = int(lbl["width"] * w), int(lbl["height"] * h)

        x1 = max(0, cx - bw // 2)
        y1 = max(0, cy - bh // 2)
        x2 = min(w, cx + bw // 2)
        y2 = min(h, cy + bh // 2)

        # 빙산: 강한 후방산란 (밝은 영역)
        brightness = np.random.uniform(0.6, 0.95)
        tile[0, y1:y2, x1:x2] = brightness  # VV
        tile[1, y1:y2, x1:x2] = brightness * 0.8  # VH (약간 낮음)
        tile[2, y1:y2, x1:x2] = np.clip(brightness / (brightness * 0.8) / 20.0, 0, 1)

    return tile


def build_dataset(
    output_dir: Path = DATASET_DIR,
    synthetic_count: int = 200,
    use_real_tiles: bool = False,
    real_tiles_dir: Path | None = None,
) -> Path:
    """
    YOLO 포맷 데이터셋을 구축합니다.

    Args:
        output_dir: 출력 디렉토리
        synthetic_count: 합성 타일 수 (실제 데이터 없을 때)
        use_real_tiles: 실제 SAR 타일 사용 여부
        real_tiles_dir: 실제 타일 디렉토리

    Returns:
        data.yaml 경로
    """
    # 디렉토리 구조 생성
    for split in ("train", "val"):
        (output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    if use_real_tiles and real_tiles_dir and real_tiles_dir.exists():
        log.info("실제 SAR 타일로 데이터셋 구축")
        _build_from_real_tiles(real_tiles_dir, output_dir)
    else:
        log.info("합성 데이터로 데이터셋 구축 (%d 타일)", synthetic_count)
        _build_synthetic(synthetic_count, output_dir)

    # data.yaml 생성
    yaml_path = output_dir / "data.yaml"
    yaml_content = (
        f"path: {output_dir.as_posix()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"\n"
        f"nc: {len(CLASSES)}\n"
        f"names: {CLASSES}\n"
    )
    yaml_path.write_text(yaml_content, encoding="utf-8")

    log.info("데이터셋 구축 완료: %s", yaml_path)
    return yaml_path


def _build_synthetic(count: int, output_dir: Path):
    """합성 데이터셋 생성."""
    labels = generate_synthetic_labels(count)
    tiles = generate_synthetic_tiles(count)

    # 빙산 주입
    for i, (tile, lbl) in enumerate(zip(tiles, labels)):
        tiles[i] = inject_synthetic_icebergs(tile, lbl["objects"])

    # Train/Val 분할
    indices = list(range(count))
    random.shuffle(indices)
    split = int(count * TRAIN_RATIO)
    train_idx, val_idx = indices[:split], indices[split:]

    for split_name, idx_list in [("train", train_idx), ("val", val_idx)]:
        for i in idx_list:
            # 이미지를 PNG로 저장 (YOLO는 .npy 직접 못 읽음)
            tile_hwc = np.transpose(tiles[i], (1, 2, 0))  # (3,H,W) → (H,W,3)
            tile_uint8 = (tile_hwc * 255).astype(np.uint8)
            img_path = output_dir / "images" / split_name / f"tile_{i:04d}.png"

            try:
                import cv2
                cv2.imwrite(str(img_path), tile_uint8)
            except ImportError:
                from PIL import Image
                Image.fromarray(tile_uint8).save(img_path)

            # 레이블 저장
            lbl_path = output_dir / "labels" / split_name / f"tile_{i:04d}.txt"
            with open(lbl_path, "w") as f:
                for obj in labels[i]["objects"]:
                    f.write(f"{obj['class']} {obj['x_center']} {obj['y_center']} {obj['width']} {obj['height']}\n")


def _build_from_real_tiles(tiles_dir: Path, output_dir: Path):
    """실제 SAR 타일로 데이터셋 구축 (Ground Truth 빙산 좌표 매칭 + PNG 변환)."""
    bergs = load_berg_ground_truth()
    npy_files = sorted(tiles_dir.glob("**/*.npy"))

    if not npy_files:
        log.warning("실제 타일을 찾을 수 없습니다: %s", tiles_dir)
        return

    # 타일 메타데이터 로드 (bbox 정보 포함)
    meta_by_name: dict[str, dict] = {}
    for mf in sorted(tiles_dir.glob("**/tile_metadata.json")):
        try:
            with open(mf) as f:
                data = json.load(f)
            for t in data.get("tiles", []):
                name = t.get("filename") or t.get("name", "")
                if name:
                    meta_by_name[name] = t
        except Exception:
            pass

    labeled_count = 0
    for npy_path in npy_files:
        try:
            tile = np.load(npy_path)
        except Exception as e:
            log.warning("타일 로드 실패 %s: %s", npy_path.name, e)
            continue

        # (C,H,W) → (H,W,C) 변환, 채널 수 정규화
        if tile.ndim == 3 and tile.shape[0] in (1, 3):
            tile_hwc = np.transpose(tile, (1, 2, 0))
        elif tile.ndim == 2:
            tile_hwc = np.stack([tile, tile, tile], axis=-1)
        else:
            tile_hwc = tile

        # 0~1 범위 → uint8 변환
        if tile_hwc.dtype != np.uint8:
            t_min, t_max = tile_hwc.min(), tile_hwc.max()
            if t_max > t_min:
                tile_hwc = ((tile_hwc - t_min) / (t_max - t_min) * 255).astype(np.uint8)
            else:
                tile_hwc = (tile_hwc * 255).clip(0, 255).astype(np.uint8)

        # 3채널 보장
        if tile_hwc.ndim == 2:
            tile_hwc = np.stack([tile_hwc, tile_hwc, tile_hwc], axis=-1)
        elif tile_hwc.shape[2] == 1:
            tile_hwc = np.concatenate([tile_hwc] * 3, axis=-1)

        split = "train" if random.random() < TRAIN_RATIO else "val"
        stem = npy_path.stem
        img_path = output_dir / "images" / split / f"{stem}.png"
        lbl_path = output_dir / "labels" / split / f"{stem}.txt"

        # PNG 저장
        try:
            import cv2
            cv2.imwrite(str(img_path), tile_hwc)
        except ImportError:
            from PIL import Image
            Image.fromarray(tile_hwc).save(img_path)

        # 메타데이터에서 bbox 정보로 빙산 좌표 매칭
        meta = meta_by_name.get(npy_path.name) or meta_by_name.get(stem)
        yolo_lines = []

        if meta and bergs:
            min_lon = meta.get("min_lon")
            max_lon = meta.get("max_lon")
            min_lat = meta.get("min_lat")
            max_lat = meta.get("max_lat")

            if None not in (min_lon, max_lon, min_lat, max_lat):
                assert min_lon is not None and max_lon is not None
                assert min_lat is not None and max_lat is not None
                lon_range = max_lon - min_lon
                lat_range = max_lat - min_lat
                if lon_range > 0 and lat_range > 0:
                    for berg in bergs:
                        blon = berg.get("lon", 0)
                        blat = berg.get("lat", 0)
                        if min_lon <= blon <= max_lon and min_lat <= blat <= max_lat:
                            cx = (blon - min_lon) / lon_range
                            cy = 1.0 - (blat - min_lat) / lat_range  # y축 반전
                            # 빙산 크기 → 타일 내 상대 크기 추정 (25m/px 기준)
                            size_m = berg.get("length_m", 100)
                            tile_h, tile_w = tile_hwc.shape[:2]
                            px_per_deg_lon = tile_w / lon_range
                            px_per_deg_lat = tile_h / lat_range
                            w = min(size_m / 111320.0 / lon_range, 0.5)
                            h = min(size_m / 110540.0 / lat_range, 0.5)
                            # 경계 범위 클리핑
                            cx = max(w / 2 + 0.01, min(1 - w / 2 - 0.01, cx))
                            cy = max(h / 2 + 0.01, min(1 - h / 2 - 0.01, cy))
                            yolo_lines.append(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
                            labeled_count += 1

        lbl_path.write_text("\n".join(yolo_lines))

    log.info("실제 타일 %d개 처리 완료, 빙산 레이블 %d개 생성", len(npy_files), labeled_count)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    yaml = build_dataset(synthetic_count=200)
    print(f"데이터셋 생성 완료: {yaml}")
