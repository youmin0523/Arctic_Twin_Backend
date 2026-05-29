-- =============================================================================
--  Digital Twin - PostgreSQL (Neon) 스키마 DDL
-- =============================================================================
--  대상 DB : Neon PostgreSQL (neondb)
--  적용    : node scripts/sync_db.js  (backend 에서 실행, 자동으로 이 DDL 적용)
--
--  포함 테이블 (backend/data 의 JSON 기반)
--    1) icebergs            <- copernicus_icebergs.json      (723건)
--    2) bergs               <- realBergData_latest.json      (85건)
--    3) sar_detections      <- sar_detections_latest.json    (탐지 이벤트/시계열)
--    4) sentinel1_products  <- sentinel1_catalog_latest.json (302건)
--    5) weather_api_usage   <- weather_api_usage.json        (일자별 카운터)
--    6) simulation_results  <- data/simulations/*.json       (JSONB)
--
--  제외: realIceData_latest.json / data/archive/* (대용량 해빙 그리드 → 파일 유지)
--  제외: model/**, pipeline/models/** (모델 산출물·학습 로그 → 파일 유지)
-- =============================================================================

-- 전체 초기화가 필요할 때만 주석 해제:
-- DROP TABLE IF EXISTS icebergs, bergs, sar_detections, sentinel1_products,
--                      weather_api_usage, simulation_results CASCADE;

-- 1) icebergs : Copernicus SAR 빙산 카탈로그
CREATE TABLE IF NOT EXISTS icebergs (
    id           TEXT PRIMARY KEY,            -- "COP-SAR-0001"
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    source       TEXT,                        -- "Copernicus SAR (Radarsat)"
    period       TEXT,                        -- "2026-01-28 / 2026-02-01"
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_icebergs_lat_lon ON icebergs (lat, lon);

-- 2) bergs : 현재 빙산 위치 (US NIC / BYU·NASA / NSIDC IIP)
CREATE TABLE IF NOT EXISTS bergs (
    id           TEXT PRIMARY KEY,            -- "A76C"
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    length_m     DOUBLE PRECISION,
    width_m      DOUBLE PRECISION,
    type         TEXT,                        -- "large" | "tabular" | ...
    last_update  DATE,                        -- 원본 "MM/DD/YYYY" → DATE
    data_source  TEXT,                        -- 데이터셋 source 메타
    data_date    DATE,                        -- 데이터셋 date 메타
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bergs_type    ON bergs (type);
CREATE INDEX IF NOT EXISTS idx_bergs_lat_lon ON bergs (lat, lon);

-- 3) sar_detections : SAR(Sentinel-1) 빙산 탐지 이벤트 (시계열)
CREATE TABLE IF NOT EXISTS sar_detections (
    pk                   BIGSERIAL PRIMARY KEY,
    detection_id         TEXT,                 -- "SAR_DEMO_001"
    lat                  DOUBLE PRECISION NOT NULL,
    lon                  DOUBLE PRECISION NOT NULL,
    length_m             DOUBLE PRECISION,
    width_m              DOUBLE PRECISION,
    type                 TEXT,
    source               TEXT,                 -- "sentinel1_sar"
    confidence           DOUBLE PRECISION,
    last_update          DATE,
    detection_time       TIMESTAMPTZ,          -- 배치 detection_time
    confidence_threshold DOUBLE PRECISION,
    products_processed   INTEGER,              -- 배치에서 처리한 SAR 영상(제품) 개수
    imported_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (detection_time, detection_id)      -- 재실행 멱등성
);
CREATE INDEX IF NOT EXISTS idx_sar_detection_time ON sar_detections (detection_time);
CREATE INDEX IF NOT EXISTS idx_sar_type           ON sar_detections (type);
-- 기존 DB(테이블 선생성됨)에도 컬럼 추가 (멱등):
ALTER TABLE sar_detections ADD COLUMN IF NOT EXISTS products_processed INTEGER;

-- 4) sentinel1_products : Sentinel-1 IW GRD 제품 카탈로그 (CDSE)
CREATE TABLE IF NOT EXISTS sentinel1_products (
    id                 TEXT PRIMARY KEY,        -- CDSE product UUID
    name               TEXT NOT NULL,
    sensing_start      TIMESTAMPTZ,
    sensing_stop       TIMESTAMPTZ,
    aoi                TEXT,                     -- "svalbard" | "greenland_east" | ...
    orbit_direction    TEXT,                     -- "" → NULL 정규화
    polarization       TEXT,                     -- "" → NULL 정규화
    file_path          TEXT,                     -- 원본 null 가능
    file_size_mb       DOUBLE PRECISION,
    download_timestamp TIMESTAMPTZ,
    imported_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_s1_aoi           ON sentinel1_products (aoi);
CREATE INDEX IF NOT EXISTS idx_s1_sensing_start ON sentinel1_products (sensing_start);

-- 5) weather_api_usage : 날씨 API 일자별 호출 카운터
CREATE TABLE IF NOT EXISTS weather_api_usage (
    usage_date  DATE PRIMARY KEY,             -- "2026-04-20"
    calls       INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6) simulation_results : 쇄빙선 에스코트 항해 시뮬 결과 (전체 페이로드 JSONB)
CREATE TABLE IF NOT EXISTS simulation_results (
    id           BIGSERIAL PRIMARY KEY,
    scenario     TEXT NOT NULL UNIQUE,         -- "nsr_month03_arc4"
    route_code   TEXT,                         -- "NSR"
    month        INTEGER,                      -- 3
    arc_level    INTEGER,                      -- 4 / 7 / 9 (ARC ice class)
    source_file  TEXT,                         -- "nsr_month03_arc4.json"
    payload      JSONB NOT NULL,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sim_route ON simulation_results (route_code, month, arc_level);
-- JSONB 내부 키 조회가 잦으면:
-- CREATE INDEX IF NOT EXISTS idx_sim_payload_gin ON simulation_results USING GIN (payload);
