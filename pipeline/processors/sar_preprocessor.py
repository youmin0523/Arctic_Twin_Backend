#!/usr/bin/env python3
"""
sar_preprocessor.py
===================
Sentinel-1 IW GRD SAR 제품을 CV 모델 입력용 타일로 전처리합니다.

파이프라인:
  1. SAFE .zip 아카이브에서 측정 GeoTIFF 추출 (VV + VH 밴드)
  2. rasterio로 GeoTIFF 읽기 → numpy 배열 (2채널)
  3. 라디오메트릭 보정: DN → σ0 (dB)
  4. 640×640 타일 분할 (10% 오버랩)
  5. 각 타일의 지리 좌표 변환 행렬 기록

출력:
  - tiles/: 640×640 numpy 타일 (3채널: VV, VH, VV/VH)
  - tile_metadata.json: 각 타일의 좌표변환 정보
"""

import json
import logging
import zipfile
from pathlib import Path

import numpy as np

log = logging.getLogger("sar_preprocessor")

TILE_SIZE = 640
TILE_OVERLAP = 0.1  # 10% 오버랩
OUTPUT_CHANNELS = 3  # VV, VH, VV/VH


def extract_tiff_from_safe(zip_path: Path, output_dir: Path) -> dict[str, Path]:
    """
    SAFE .zip에서 measurement/*.tiff 파일을 추출합니다.

    Returns:
        {"vv": Path, "vh": Path} 또는 {"hh": Path, "hv": Path}
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted = {}

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            lower = name.lower()
            if "/measurement/" not in lower or not lower.endswith(".tiff"):
                continue

            # 편파 채널 식별
            for pol in ("vv", "vh", "hh", "hv"):
                if f"-{pol}-" in lower or f"_{pol}_" in lower or lower.endswith(f"-{pol}.tiff"):
                    out_path = output_dir / f"{pol}.tiff"
                    with zf.open(name) as src, open(out_path, "wb") as dst:
                        dst.write(src.read())
                    extracted[pol] = out_path
                    log.info("추출: %s → %s", name, out_path)
                    break

    if not extracted:
        log.warning("SAFE 아카이브에서 측정 TIFF를 찾을 수 없습니다: %s", zip_path.name)

    return extracted


def read_geotiff(tiff_path: Path) -> tuple[np.ndarray, dict]:
    """
    GeoTIFF를 읽어 numpy 배열 + 좌표 변환 정보를 반환합니다.

    Returns:
        (array: np.ndarray, geo_info: dict)
        geo_info: {"transform": list, "crs": str, "width": int, "height": int}
    """
    try:
        import rasterio
    except ImportError:
        raise ImportError(
            "rasterio가 필요합니다: pip install rasterio\n"
            "Windows에서는 conda install -c conda-forge rasterio 권장"
        )

    with rasterio.open(tiff_path) as src:
        data = src.read(1).astype(np.float32)  # 단일 밴드
        transform = list(src.transform)[:6]  # Affine 6 파라미터
        crs = str(src.crs) if src.crs else "EPSG:4326"
        geo_info = {
            "transform": transform,
            "crs": crs,
            "width": src.width,
            "height": src.height,
        }

    return data, geo_info


def calibrate_to_sigma0(dn_array: np.ndarray) -> np.ndarray:
    """
    디지털 넘버(DN)를 σ0(dB)로 라디오메트릭 보정합니다.

    Sentinel-1 GRD의 간략 보정 공식:
      σ0 = 10 × log10(DN²) - 노이즈 보정
    여기서는 간략화하여 DN→dB 변환만 수행합니다.
    (정밀 보정은 ESA SNAP 도구가 필요하지만, 빙산 탐지에는 이 수준으로 충분)
    """
    # DN이 0인 픽셀은 마스킹 (No Data)
    mask = dn_array > 0
    sigma0 = np.zeros_like(dn_array)
    sigma0[mask] = 10.0 * np.log10(dn_array[mask] ** 2 + 1e-10)

    # dB 범위 클리핑: [-30, 10] dB (SAR 일반 범위)
    sigma0 = np.clip(sigma0, -30.0, 10.0)

    # 0~1 정규화 (모델 입력용)
    sigma0 = (sigma0 - (-30.0)) / (10.0 - (-30.0))

    return sigma0


def build_three_channel(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    """
    VV, VH 2채널 SAR → 3채널 입력 생성.

    채널 구성: [VV, VH, VV/VH]
    VV/VH 비율은 해수면(낮음) vs 빙산(높음)을 구분하는 핵심 특성입니다.
    """
    # 0 나누기 방지
    ratio = np.where(vh > 1e-6, vv / (vh + 1e-6), 0.0)
    # 비율 정규화: 일반적으로 1~20 범위 → 0~1
    ratio = np.clip(ratio / 20.0, 0.0, 1.0)

    return np.stack([vv, vh, ratio], axis=0)  # (3, H, W)


def tile_image(
    image: np.ndarray,
    geo_info: dict,
    tile_size: int = TILE_SIZE,
    overlap: float = TILE_OVERLAP,
) -> list[dict]:
    """
    3채널 이미지를 640×640 타일로 분할합니다.

    Args:
        image: (3, H, W) numpy 배열
        geo_info: 좌표 변환 정보
        tile_size: 타일 크기 (기본 640)
        overlap: 오버랩 비율 (기본 0.1)

    Returns:
        [{"tile": np.ndarray, "row": int, "col": int, "geo_offset": dict}, ...]
    """
    _, h, w = image.shape
    step = int(tile_size * (1 - overlap))

    tiles = []
    row_idx = 0
    for y in range(0, h - tile_size + 1, step):
        col_idx = 0
        for x in range(0, w - tile_size + 1, step):
            tile = image[:, y : y + tile_size, x : x + tile_size]

            # 유효 픽셀 비율 확인 (최소 30% 이상)
            valid_ratio = np.mean(tile[0] > 0)
            if valid_ratio < 0.3:
                col_idx += 1
                continue

            # 타일의 지리 좌표 오프셋 계산
            transform = geo_info["transform"]
            tile_geo = {
                "pixel_x": x,
                "pixel_y": y,
                "origin_lon": transform[2] + x * transform[0],
                "origin_lat": transform[5] + y * transform[4],
                "pixel_size_x": transform[0],
                "pixel_size_y": transform[4],
            }

            tiles.append({
                "tile": tile,
                "row": row_idx,
                "col": col_idx,
                "geo_offset": tile_geo,
            })
            col_idx += 1
        row_idx += 1

    log.info("타일 생성: %d개 (원본 %dx%d, 타일 %d, 스텝 %d)", len(tiles), w, h, tile_size, step)
    return tiles


def preprocess_product(zip_path: Path, output_dir: Path) -> list[dict]:
    """
    Sentinel-1 GRD 제품 하나를 전처리하여 타일 목록을 반환합니다.

    전체 파이프라인:
      .zip → TIFF 추출 → GeoTIFF 읽기 → 보정 → 3채널 → 타일링

    Args:
        zip_path: SAFE .zip 파일 경로
        output_dir: 중간 파일 저장 디렉토리

    Returns:
        타일 딕셔너리 목록 [{"tile": ndarray(3,640,640), "geo_offset": {...}}, ...]
    """
    product_name = zip_path.stem
    work_dir = output_dir / product_name
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. TIFF 추출
    log.info("[1/4] SAFE 아카이브에서 TIFF 추출: %s", zip_path.name)
    tiffs = extract_tiff_from_safe(zip_path, work_dir)

    # VV/VH 우선, HH/HV 폴백
    if "vv" in tiffs and "vh" in tiffs:
        co_pol, cross_pol = "vv", "vh"
    elif "hh" in tiffs and "hv" in tiffs:
        co_pol, cross_pol = "hh", "hv"
    else:
        log.error("이중 편파 채널을 찾을 수 없습니다: %s", list(tiffs.keys()))
        return []

    # 2. GeoTIFF 읽기
    log.info("[2/4] GeoTIFF 읽기: %s + %s", co_pol, cross_pol)
    co_data, geo_info = read_geotiff(tiffs[co_pol])
    cross_data, _ = read_geotiff(tiffs[cross_pol])

    # 3. 라디오메트릭 보정
    log.info("[3/4] 라디오메트릭 보정 (DN → σ0)")
    co_sigma = calibrate_to_sigma0(co_data)
    cross_sigma = calibrate_to_sigma0(cross_data)

    # 4. 3채널 합성 + 타일링
    log.info("[4/4] 3채널 합성 + 타일링")
    three_ch = build_three_channel(co_sigma, cross_sigma)
    tiles = tile_image(three_ch, geo_info)

    # 타일 메타데이터 저장
    meta_path = work_dir / "tile_metadata.json"
    meta = [
        {
            "index": i,
            "row": t["row"],
            "col": t["col"],
            "geo_offset": t["geo_offset"],
            "shape": list(t["tile"].shape),
        }
        for i, t in enumerate(tiles)
    ]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"product": product_name, "tile_count": len(tiles), "tiles": meta}, f, indent=2)

    log.info("전처리 완료: %s → %d 타일", product_name, len(tiles))
    return tiles


def save_tiles_as_npy(tiles: list[dict], output_dir: Path) -> list[Path]:
    """타일을 개별 .npy 파일로 저장합니다 (학습/추론용)."""
    npy_dir = output_dir / "tiles_npy"
    npy_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for i, t in enumerate(tiles):
        p = npy_dir / f"tile_{i:04d}.npy"
        np.save(p, t["tile"])
        paths.append(p)

    return paths


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("사용법: python sar_preprocessor.py <safe_product.zip>")
        sys.exit(1)

    zip_file = Path(sys.argv[1])
    if not zip_file.exists():
        print(f"파일을 찾을 수 없습니다: {zip_file}")
        sys.exit(1)

    out = Path(__file__).parent.parent.parent / "data" / "sar_processed"
    tiles = preprocess_product(zip_file, out)
    if tiles:
        saved = save_tiles_as_npy(tiles, out / zip_file.stem)
        print(f"저장 완료: {len(saved)}개 타일 → {saved[0].parent}")
