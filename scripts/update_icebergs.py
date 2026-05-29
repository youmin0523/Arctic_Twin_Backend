#!/usr/bin/env python3
"""
Copernicus Marine Service — Arctic iceberg data pipeline
Downloads latest SAR-based iceberg shapefiles, converts to JSON.
Runs daily at 03:00 UTC via node-schedule or standalone cron.
"""

import os
import json
import logging
import tempfile
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "iceberg_update.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("iceberg_pipeline")

# ── Paths ────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "copernicus_icebergs.json"

# ── Copernicus datasets ─────────────────────────────────────────
DATASETS = [
    {
        "id": "cmems_obs-si_arc_phy_berg-point-mos-rcmln_nrt_l3_P1D_irr",
        "source_label": "Copernicus SAR (Radarsat)",
        "id_prefix": "COP-SAR",
    },
    {
        "id": "cmems_sat-si_arc_berg-point_nrt_ew_d",
        "source_label": "Copernicus SAR (Sentinel-1 EW)",
        "id_prefix": "COP-EW",
    },
    {
        "id": "cmems_sat-si_arc_berg-point_nrt_iw_d",
        "source_label": "Copernicus SAR (Sentinel-1 IW)",
        "id_prefix": "COP-IW",
    },
]


def download_dataset(dataset_id: str, output_dir: str) -> str | None:
    """Download latest shapefile from Copernicus Marine."""
    try:
        import copernicusmarine as cm

        log.info(f"Downloading {dataset_id} ...")
        result = cm.get(
            dataset_id=dataset_id,
            output_directory=output_dir,
            overwrite=True,
        )
        log.info(f"  → Download result: {result}")
        return output_dir
    except Exception as e:
        log.warning(f"  → Download failed for {dataset_id}: {e}")
        return None


def extract_zips(directory: str):
    """Extract all .zip files found in directory tree."""
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".zip"):
                zip_path = os.path.join(root, f)
                try:
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(root)
                    log.info(f"  Extracted: {zip_path}")
                except Exception as e:
                    log.warning(f"  Failed to extract {zip_path}: {e}")


def find_shapefiles(directory: str) -> list[str]:
    """Recursively find all .shp files in directory."""
    shp_files = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".shp"):
                shp_files.append(os.path.join(root, f))
    return shp_files


def shapefile_to_icebergs(shp_path: str, source_label: str, id_prefix: str) -> list[dict]:
    """Convert a shapefile to iceberg JSON entries."""
    import geopandas as gpd

    log.info(f"  Reading {shp_path}")
    gdf = gpd.read_file(shp_path)
    log.info(f"  → {len(gdf)} features found")

    icebergs = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        lat = geom.y if hasattr(geom, "y") else geom.centroid.y
        lon = geom.x if hasattr(geom, "x") else geom.centroid.x

        # Skip southern hemisphere
        if lat < 0:
            continue

        # Extract period from attributes if available
        period = ""
        for col in ["time", "date", "DATE", "TIME", "period", "PERIOD"]:
            if col in gdf.columns and row[col] is not None:
                period = str(row[col])
                break

        icebergs.append({
            "id": f"{id_prefix}-{idx + 1:04d}",
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "source": source_label,
            "period": period,
        })

    log.info(f"  → {len(icebergs)} northern hemisphere icebergs")
    return icebergs


def deduplicate(icebergs: list[dict], threshold_deg: float = 0.05) -> list[dict]:
    """Remove near-duplicate icebergs (within threshold degrees)."""
    unique = []
    for berg in icebergs:
        is_dup = False
        for u in unique:
            if abs(berg["lat"] - u["lat"]) < threshold_deg and abs(berg["lon"] - u["lon"]) < threshold_deg:
                is_dup = True
                break
        if not is_dup:
            unique.append(berg)
    removed = len(icebergs) - len(unique)
    if removed > 0:
        log.info(f"Deduplication: {len(icebergs)} → {len(unique)} ({removed} duplicates removed)")
    return unique


def main():
    log.info("=" * 60)
    log.info("Arctic Iceberg Pipeline — Starting")
    log.info("=" * 60)

    all_icebergs = []
    sources_used = []
    tmpdir = tempfile.mkdtemp(prefix="cop_icebergs_")

    try:
        for ds in DATASETS:
            ds_dir = os.path.join(tmpdir, ds["id_prefix"])
            os.makedirs(ds_dir, exist_ok=True)

            result = download_dataset(ds["id"], ds_dir)
            if result is None:
                continue

            extract_zips(ds_dir)
            shp_files = find_shapefiles(ds_dir)
            if not shp_files:
                log.warning(f"  No .shp files found for {ds['id']}")
                continue

            for shp in shp_files:
                bergs = shapefile_to_icebergs(shp, ds["source_label"], ds["id_prefix"])
                all_icebergs.extend(bergs)
                sources_used.append(ds["source_label"])

    except Exception as e:
        log.error(f"Pipeline error: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not all_icebergs:
        log.warning("No icebergs downloaded — keeping existing JSON file")
        return

    # Deduplicate across sources
    all_icebergs = deduplicate(all_icebergs)

    # Re-number IDs sequentially
    for i, berg in enumerate(all_icebergs):
        prefix = berg["id"].rsplit("-", 1)[0]
        berg["id"] = f"{prefix}-{i + 1:04d}"

    # Build output JSON
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output = {
        "icebergs": all_icebergs,
        "count": len(all_icebergs),
        "updated_at": now,
        "sources": list(set(sources_used)),
    }

    # Atomic write (write to temp then rename)
    tmp_out = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp_out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)
    tmp_out.replace(OUTPUT_FILE)

    log.info(f"SUCCESS — {output['count']} icebergs written to {OUTPUT_FILE}")
    log.info(f"Sources: {output['sources']}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
