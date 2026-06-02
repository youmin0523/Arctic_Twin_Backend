/**
 * OpenAPI 3.0 명세 — Arctic Digital Twin API Gateway (8000)
 *
 * 본 명세는 docs/API설계서_ArcticDigitalTwin_v1.0.md 의 계약을 기계 판독 가능한
 * OpenAPI 3.0.3 형식으로 옮긴 것이다. Swagger UI(/api-docs)로 서빙된다.
 *
 * servers 배열에 로컬 개발 서버와 운영(배포) 서버를 모두 등록하여, Swagger UI
 * 우측 상단 드롭다운에서 호출 대상을 전환할 수 있다.
 */

const SERVERS = [
  {
    url: 'http://localhost:8000',
    description: '개발(Local) — Node Gateway 직접 접근',
  },
  {
    url: 'https://arctictwin.com',
    description: '운영(Production) — Vercel 도메인, rewrite 통해 백엔드로 프록시',
  },
  {
    url: 'https://api.arctictwin.com',
    description: '운영(Production) — 백엔드 게이트웨이 직접(AWS EC2, Vercel rewrite 목적지)',
  },
];

// ── 공통 스키마 ─────────────────────────────────────────────────
const components = {
  schemas: {
    Error: {
      type: 'object',
      properties: {
        error: { type: 'string', description: '사람이 읽을 수 있는 오류 메시지', example: 'month parameter is invalid' },
        code: { type: 'integer', description: 'HTTP 상태 코드', example: 400 },
        detail: { type: 'string', description: '(선택) 추가 진단 정보', example: "expected YYYY-MM or 'latest'" },
      },
      required: ['error', 'code'],
    },
    LonLat: {
      type: 'object',
      properties: {
        lon: { type: 'number', example: 30.1 },
        lat: { type: 'number', example: 78.0 },
      },
      required: ['lon', 'lat'],
    },
    Waypoint: {
      type: 'object',
      properties: {
        lon: { type: 'number', example: 30.1 },
        lat: { type: 'number', example: 78.0 },
        label: { type: 'string', example: 'Murmansk 출발' },
      },
      required: ['lon', 'lat'],
    },
    Iceberg: {
      type: 'object',
      properties: {
        id: { type: 'string', example: 'B-2026-0091' },
        lat: { type: 'number', example: 79.13 },
        lon: { type: 'number', example: 25.88 },
        length_m: { type: 'number', example: 420 },
        source: { type: 'string', enum: ['nic', 'copernicus', 'sar'], example: 'nic' },
      },
    },
    JobAccepted: {
      type: 'object',
      properties: {
        job_id: { type: 'string', example: 'rpt-20260602-7f3a' },
        message: { type: 'string', example: 'report generation started' },
      },
    },
    JobStatus: {
      type: 'object',
      properties: {
        status: { type: 'string', enum: ['queued', 'running', 'completed', 'failed'], example: 'running' },
        progress: { type: 'number', format: 'float', minimum: 0, maximum: 1, example: 0.62 },
        error: { type: 'string', nullable: true, example: null },
      },
    },
    Vessel: {
      type: 'object',
      properties: {
        iceClass: { type: 'string', example: 'PC6' },
        displacement: { type: 'number', example: 85000 },
        draft: { type: 'number', example: 11.5 },
        enginePower: { type: 'number', example: 21000 },
      },
      required: ['iceClass', 'displacement'],
    },
    FuelInput: {
      type: 'object',
      properties: {
        displacement: { type: 'number', example: 85000, description: '배수량(톤)' },
        draft: { type: 'number', example: 11.5, description: '흘수(m)' },
        engine_power: { type: 'number', example: 21000, description: '엔진 출력(kW)' },
        ice_thickness: { type: 'number', example: 1.2, description: '해빙 두께(m)' },
        ice_concentration: { type: 'number', example: 0.6, description: '해빙 농도(0~1)' },
        ice_class_code: { type: 'integer', example: 6, description: '빙급 코드(예: PC6→6)' },
      },
      required: ['displacement', 'draft', 'engine_power', 'ice_thickness', 'ice_concentration', 'ice_class_code'],
    },
  },
  responses: {
    BadRequest: { description: '잘못된 요청', content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' } } } },
    NotFound: { description: '자원 없음', content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' } } } },
    Conflict: { description: '충돌(이미 학습 진행 중 등)', content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' } } } },
    ServiceUnavailable: { description: '의존 마이크로서비스 전체 장애', content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' } } } },
  },
};

// ── 헬퍼: 표준 응답 ──────────────────────────────────────────────
const ok = (description, example) => ({
  200: {
    description,
    content: { 'application/json': example !== undefined ? { schema: { type: 'object' }, example } : { schema: { type: 'object' } } },
  },
});

const accepted = (example) => ({
  202: {
    description: '접수됨 — 비동기 작업이 백그라운드로 시작됨',
    content: { 'application/json': { schema: { type: 'object' }, example } },
  },
});

const jsonBody = (example, schemaRef) => ({
  required: true,
  content: {
    'application/json': {
      schema: schemaRef ? { $ref: schemaRef } : { type: 'object' },
      example,
    },
  },
});

const monthParam = {
  name: 'month',
  in: 'query',
  required: false,
  schema: { type: 'string' },
  description: '`YYYY-MM` 또는 `latest`. 생략 시 `latest`.',
  example: '2026-05',
};

const jobIdPath = {
  name: 'job_id',
  in: 'path',
  required: true,
  schema: { type: 'string' },
  description: '비동기 작업 ID',
};

// ── 경로 정의 ────────────────────────────────────────────────────
const paths = {
  // 4.1 시스템 / 헬스
  '/api/health': {
    get: {
      tags: ['System / Health'],
      summary: '게이트웨이 헬스 체크',
      description: '게이트웨이 생존 여부 확인. 로드밸런서/모니터링 헬스 프로브용.',
      responses: ok('정상', { status: 'ok', timestamp: '2026-06-02T09:15:22Z' }),
    },
  },
  '/api/health/services': {
    get: {
      tags: ['System / Health'],
      summary: '마이크로서비스 readiness',
      description: '내부 마이크로서비스(8001/8002/8003) readiness 일괄 조회. `false`인 서비스는 graceful degradation 대상.',
      responses: ok('각 서비스 readiness(boolean)', { rl: true, report: true, ml: false }),
    },
  },

  // 4.2 해빙 · 빙산 · 기상
  '/api/ice/concentration': {
    get: {
      tags: ['Ice / Iceberg / Weather'],
      summary: '해빙 농도 격자',
      description: '월별 해빙 농도(sea ice concentration) 격자. DB 미존재 시 JSON 폴백(`source:"file"`).',
      parameters: [monthParam],
      responses: ok('해빙 농도 격자', {
        source: 'db', month: '2026-05', updated_at: '2026-05-31T00:00:00Z',
        grid: { type: 'FeatureCollection', features: [{ lon: 30.5, lat: 78.2, concentration: 0.92 }] },
      }),
    },
  },
  '/api/ice/thickness': {
    get: {
      tags: ['Ice / Iceberg / Weather'],
      summary: '해빙 두께 격자',
      description: '월별 해빙 두께(m) 격자. 항로 평가 및 연료 예측 입력 피처로 활용.',
      parameters: [monthParam],
      responses: ok('해빙 두께 격자', {
        source: 'db', month: '2026-05', updated_at: '2026-05-31T00:00:00Z',
        grid: { features: [{ lon: 30.5, lat: 78.2, thickness_m: 1.84 }] },
      }),
    },
  },
  '/api/ice/archives': {
    get: {
      tags: ['Ice / Iceberg / Weather'],
      summary: '해빙 아카이브 목록',
      description: '조회 가능한 해빙 데이터 아카이브(월별 + 일별) 목록. 프론트엔드 드롭다운 구성용.',
      responses: ok('아카이브 엔트리', {
        entries: [
          { value: 'latest', label: '최신' },
          { value: '2026-05', label: '2026년 5월' },
          { value: '2026-05-30', label: '2026-05-30 (일별)' },
        ],
      }),
    },
  },
  '/api/icebergs/latest': {
    get: {
      tags: ['Ice / Iceberg / Weather'],
      summary: '빙산 최신 데이터',
      description: '최신 빙산 위치 데이터(NIC + Copernicus 통합). `berg_count = nic_count + copernicus_count`.',
      responses: ok('최신 빙산', {
        source: 'db', date: '2026-05-30', updated_at: '2026-05-30T11:42:18Z',
        berg_count: 137, nic_count: 92, copernicus_count: 45,
        bergs: [{ id: 'B-2026-0091', lat: 79.13, lon: 25.88, length_m: 420, source: 'nic' }],
      }),
    },
  },
  '/api/weather/latest': {
    get: {
      tags: ['Ice / Iceberg / Weather'],
      summary: '기상 예보 최신',
      description: '5개 주요 북극항로 기상 예보(Open-Meteo). 게이트웨이에서 10분 캐시 적용.',
      responses: ok('기상 예보', {
        source: 'open-meteo', fetched_at: '2026-06-02T09:05:00Z',
        routes: [{ route: 'NSR', forecast: [{ time: '2026-06-02T12:00', wind_speed_ms: 7.2, wave_height_m: 1.4, visibility_km: 8.5, temperature_c: -2.1 }] }],
      }),
    },
  },

  // 4.3 Sentinel-1
  '/api/sentinel1/catalog': {
    get: {
      tags: ['Sentinel-1'],
      summary: 'Sentinel-1 카탈로그',
      description: '보유 중인 Sentinel-1 SAR 제품 카탈로그 전체.',
      responses: ok('카탈로그', {
        source: 'sentinel1',
        products: [{ id: 'S1A_IW_GRDH_20260528T064210', sensing_date: '2026-05-28', aoi: 'svalbard', polarization: 'HH+HV', footprint: 'POLYGON((...))' }],
      }),
    },
  },
  '/api/sentinel1/products': {
    get: {
      tags: ['Sentinel-1'],
      summary: 'Sentinel-1 제품 필터 조회',
      description: 'AOI 및 기간 조건으로 Sentinel-1 제품 필터 조회.',
      parameters: [
        { name: 'aoi', in: 'query', required: false, schema: { type: 'string' }, example: 'svalbard', description: '관심영역' },
        { name: 'from', in: 'query', required: false, schema: { type: 'string', format: 'date' }, example: '2026-05-01', description: '시작일 YYYY-MM-DD' },
        { name: 'to', in: 'query', required: false, schema: { type: 'string', format: 'date' }, example: '2026-05-31', description: '종료일 YYYY-MM-DD' },
      ],
      responses: ok('필터 결과', {
        source: 'sentinel1', filter: { aoi: 'svalbard', from: '2026-05-01', to: '2026-05-31' },
        product_count: 12, products: [{ id: 'S1A_IW_GRDH_20260528T064210', sensing_date: '2026-05-28', aoi: 'svalbard' }],
      }),
    },
  },

  // 4.4 항로 평가 · 편집
  '/api/route/evaluate': {
    post: {
      tags: ['Route'],
      summary: '항로 평가',
      description: '주어진 항로·선박·월 조건에 대한 종합 항로 평가(arctic_master_router 실행).',
      requestBody: jsonBody({
        route: [{ lon: 30.1, lat: 78.0 }, { lon: 60.5, lat: 75.2 }, { lon: 120.3, lat: 72.8 }],
        vessel: { iceClass: 'PC6', displacement: 85000, draft: 11.5, enginePower: 21000 },
        month: '2026-06',
      }),
      responses: ok('항로 평가 결과', {
        route_id: 'eval-20260602-001', feasible: true, risk_score: 0.34,
        segments: [{ from: 0, to: 1, ice_concentration: 0.61, risk: 'medium' }],
        estimated_transit_days: 9.2, warnings: ['high ice concentration near 75N'],
      }),
    },
  },
  '/api/pipeline/run': {
    post: {
      tags: ['Route'],
      summary: '데이터 파이프라인 수동 실행',
      description: '데이터 수집 파이프라인 수동 트리거. 평상시엔 스케줄러가 자동 수집.',
      requestBody: jsonBody({ task: 'fetch_ice' }),
      responses: ok('실행 결과', { status: 'ok', output: 'fetched ice grids for 2026-05; migrated 3 archives to DB' }),
    },
  },
  '/api/routes/edited': {
    get: {
      tags: ['Route'],
      summary: '편집 항로 조회',
      description: '사용자가 편집·저장한 항로 경유점 조회. DB 우선, 실패 시 파일 폴백.',
      responses: ok('항로별 경유점', {
        NSR: [{ lon: 30.1, lat: 78.0, label: 'Murmansk 출발' }, { lon: 60.5, lat: 75.2, label: 'Kara Sea' }],
        NWP: [{ lon: -55.0, lat: 70.0, label: 'Baffin Bay' }],
      }),
    },
    post: {
      tags: ['Route'],
      summary: '편집 항로 저장(upsert)',
      description: '편집된 항로 전체 교체 저장. 전송된 항로 집합이 기존 저장본을 대체한다.',
      requestBody: jsonBody({
        NSR: [{ lon: 30.1, lat: 78.0, label: 'Murmansk 출발' }, { lon: 60.5, lat: 75.2, label: 'Kara Sea' }],
        NWP: [{ lon: -55.0, lat: 70.0, label: 'Baffin Bay' }],
      }),
      responses: ok('저장 결과', { ok: true, routes: ['NSR', 'NWP'], store: 'db' }),
    },
  },
  '/api/avoidance/log': {
    post: {
      tags: ['Route'],
      summary: '회피 로그 기록',
      description: '빙산 회피 이벤트 스냅샷을 JSONL 파일에 누적 추가(append).',
      requestBody: jsonBody({
        timestamp: '2026-06-02T10:12:00Z',
        ship_state: { lon: 60.5, lat: 75.2, heading: 95, speed_knots: 11.0 },
        iceberg: { lat: 75.25, lon: 60.6, length_m: 320 },
        action: { heading_delta: -12, speed_factor: 0.8 },
        collision_risk: 0.27,
      }),
      responses: ok('기록 완료', { ok: true }),
    },
  },
  '/api/avoidance/summary': {
    get: {
      tags: ['Route'],
      summary: '회피 통계 요약',
      description: '누적된 회피 로그 통계 집계. 회피 효과 분석 대시보드용.',
      responses: ok('통계', {
        total_events: 218, avg_collision_risk: 0.31, avg_heading_delta: -8.4,
        max_collision_risk: 0.82, by_route: { NSR: 140, NWP: 78 },
      }),
    },
  },

  // 4.5 시뮬레이션
  '/api/simulations': {
    get: {
      tags: ['Simulation'],
      summary: '시뮬레이션 목록',
      description: '저장된 시뮬레이션 시나리오 목록.',
      responses: ok('시나리오 목록', {
        source: 'db', count: 3,
        scenarios: [{ scenario: 'nsr-pc6-june', title: 'NSR PC6 6월 통항', created_at: '2026-05-29T08:00:00Z' }],
      }),
    },
  },
  '/api/simulations/{scenario}': {
    get: {
      tags: ['Simulation'],
      summary: '시뮬레이션 상세',
      description: '특정 시나리오의 전체 payload 조회. 미존재 시 404.',
      parameters: [{ name: 'scenario', in: 'path', required: true, schema: { type: 'string' }, example: 'nsr-pc6-june', description: '시나리오 식별자' }],
      responses: {
        ...ok('시나리오 상세', {
          scenario: 'nsr-pc6-june', title: 'NSR PC6 6월 통항',
          payload: { route: [{ lon: 30.1, lat: 78.0 }], vessel: { iceClass: 'PC6' }, timeline: [{ t: 0, lon: 30.1, lat: 78.0, speed_knots: 11.0 }] },
        }),
        404: { $ref: '#/components/responses/NotFound' },
      },
    },
  },

  // 4.6 협업 / SAR
  '/api/collab/sar-icebergs': {
    get: {
      tags: ['Collab / SAR'],
      summary: 'SAR 탐지 빙산',
      description: 'Sentinel-1 SAR(YOLOv8) 탐지 빙산 결과. 신뢰도(confidence) 포함.',
      responses: ok('SAR 탐지 빙산', {
        source: 'sentinel1_sar (YOLOv8)', berg_count: 24, updated_at: '2026-05-28T07:10:00Z',
        bergs: [{ id: 'SAR-0007', lat: 78.91, lon: 21.44, length_m: 260, confidence: 0.88 }],
      }),
    },
  },
  '/api/collab/sar-metadata': {
    get: {
      tags: ['Collab / SAR'],
      summary: 'SAR 메타데이터',
      description: '마지막 SAR 탐지 실행 메타데이터.',
      responses: ok('SAR 메타', { timestamp: '2026-05-28T07:10:00Z', confidence: 0.85, product_id: 'S1A_IW_GRDH_20260528T064210', detection_count: 24 }),
    },
  },
  '/api/collab/all-icebergs': {
    get: {
      tags: ['Collab / SAR'],
      summary: '통합 빙산 (전체 소스)',
      description: 'NIC + Copernicus + SAR 전체 소스 통합 빙산. `berg_count = nic + copernicus + sar`.',
      responses: ok('통합 빙산', {
        berg_count: 161, nic_count: 92, copernicus_count: 45, sar_count: 24,
        bergs: [
          { id: 'B-2026-0091', lat: 79.13, lon: 25.88, length_m: 420, source: 'nic' },
          { id: 'SAR-0007', lat: 78.91, lon: 21.44, length_m: 260, source: 'sar' },
        ],
      }),
    },
  },
  '/api/collab/sar-detect-trigger': {
    post: {
      tags: ['Collab / SAR'],
      summary: 'SAR 탐지 트리거',
      description: 'SAR 빙산 탐지(iceberg_detector.py) 백그라운드 실행. 완료 후 /api/collab/sar-icebergs로 폴링.',
      requestBody: jsonBody({ confidence: 0.8, max_products: 5 }),
      responses: accepted({ message: 'SAR detection started', pid: 41822, args: ['--confidence', '0.8', '--max-products', '5'] }),
    },
  },

  // 4.7 통합 AI 의사결정
  '/api/ai/navigation': {
    post: {
      tags: ['AI Decision'],
      summary: 'AI 항행 의사결정',
      description: '회피 RL(8001) + 출항 리포트(8002) + 연료 예측(8003)을 병렬 호출하는 통합 AI 의사결정. 일부 장애 시 status:"partial" + degraded[], 전체 불가 시 503.',
      requestBody: jsonBody({
        avoidance: { ship_state: { lon: 60.5, lat: 75.2, heading: 95, speed_knots: 11.0, ice_class: 'PC6', progress: 0.4 }, icebergs: [{ lat: 75.25, lon: 60.6, length_m: 320 }] },
        departure: { route: 'NSR', ice_class: 'PC6', departure_date_start: '2026-06-10' },
        fuel: { displacement: 85000, draft: 11.5, engine_power: 21000, ice_thickness: 1.2, ice_concentration: 0.6, ice_class_code: 6 },
      }),
      responses: {
        ...ok('통합 결과(부분 degradation 예시)', {
          status: 'partial',
          results: { avoidance: { recommended_heading: 83, recommended_speed_knots: 8.8, collision_risk: 0.21 }, fuel: { fuel_per_nm: 0.42, unit: 'tons/nm' } },
          degraded: ['departure'], message: 'report-service unavailable; returning partial result',
        }),
        503: { $ref: '#/components/responses/ServiceUnavailable' },
      },
    },
  },

  // 4.8 빙산회피 RL (8001)
  '/api/rl/health': {
    get: { tags: ['RL — 빙산회피(8001)'], summary: '회피 RL 헬스', responses: ok('상태', { status: 'ok', model_loaded: true }) },
  },
  '/api/rl/infer': {
    post: {
      tags: ['RL — 빙산회피(8001)'],
      summary: '회피 추론',
      description: '현재 선박/빙산/해빙/기상 상태에 대한 회피 액션 추론(SAC 정책).',
      requestBody: jsonBody({
        ship_state: { lon: 60.5, lat: 75.2, heading: 95, speed_knots: 11.0, ice_class: 'PC6', progress: 0.4 },
        icebergs: [{ lat: 75.25, lon: 60.6, length_m: 320 }],
        ice_data: { concentration: 0.6 },
        weather: { visibility_km: 8.5, wave_height_m: 1.4 },
      }),
      responses: ok('회피 액션', {
        recommended_heading: 83, recommended_speed_knots: 8.8, heading_delta: -12, speed_factor: 0.8, collision_risk: 0.21,
        path_preview: [{ lon: 60.5, lat: 75.2 }, { lon: 60.7, lat: 75.18 }],
      }),
    },
  },
  '/api/rl/train': {
    post: {
      tags: ['RL — 빙산회피(8001)'],
      summary: '회피 RL 학습',
      description: '회피 RL 학습 시작. 이미 학습 중이면 409.',
      requestBody: jsonBody({ difficulty: 'medium', timesteps: 200000, curriculum: true }),
      responses: { ...ok('학습 시작', { status: 'started', message: 'training started' }), 409: { $ref: '#/components/responses/Conflict' } },
    },
  },
  '/api/rl/status': {
    get: { tags: ['RL — 빙산회피(8001)'], summary: '회피 RL 학습 상태', responses: ok('상태', { status: 'running', progress: 0.55, current_timestep: 110000, total_timesteps: 200000 }) },
  },
  '/api/rl/stop': {
    post: { tags: ['RL — 빙산회피(8001)'], summary: '회피 RL 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },
  '/api/rl/evaluate': {
    post: {
      tags: ['RL — 빙산회피(8001)'],
      summary: '회피 RL 평가',
      parameters: [
        { name: 'n_episodes', in: 'query', required: false, schema: { type: 'integer' }, example: 50, description: '평가 에피소드 수' },
        { name: 'difficulty', in: 'query', required: false, schema: { type: 'string' }, example: 'medium', description: '평가 난이도' },
      ],
      responses: ok('평가 결과', { success_rate: 0.91, collision_rate: 0.04, avg_reward: 215.3, n_episodes: 50 }),
    },
  },
  '/api/rl/train/iterative': {
    post: {
      tags: ['RL — 빙산회피(8001)'],
      summary: '반복(iterative) 학습 시작',
      description: '목표 성능 도달까지 반복 학습.',
      requestBody: jsonBody({ max_iterations: 10, target_success_rate: 0.95, target_collision_rate: 0.03, eval_episodes: 50 }),
      responses: ok('시작됨', { status: 'started' }),
    },
  },
  '/api/rl/train/iterative/status': {
    get: { tags: ['RL — 빙산회피(8001)'], summary: '반복 학습 상태', responses: ok('상태', { status: 'running', iteration: 3, max_iterations: 10, best_success_rate: 0.89, history: [{ iteration: 1, success_rate: 0.74 }] }) },
  },
  '/api/rl/train/iterative/stop': {
    post: { tags: ['RL — 빙산회피(8001)'], summary: '반복 학습 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },
  '/api/rl/multi/train': {
    post: {
      tags: ['RL — 빙산회피(8001)'],
      summary: '멀티모델 학습',
      description: '항로×빙급×선종 조합별 개별 모델 학습.',
      requestBody: jsonBody({ routes: ['NSR', 'NWP'], ice_classes: ['PC6', 'PC7'], vessel_types: ['container', 'lng'], timesteps_per_model: 150000 }),
      responses: ok('시작됨', { status: 'started', total_models: 8 }),
    },
  },
  '/api/rl/multi/status': {
    get: { tags: ['RL — 빙산회피(8001)'], summary: '멀티모델 상태', responses: ok('상태', { status: 'running', total_models: 8, completed_models: 3, current: { route: 'NSR', ice_class: 'PC7', vessel_type: 'lng' } }) },
  },
  '/api/rl/multi/stop': {
    post: { tags: ['RL — 빙산회피(8001)'], summary: '멀티모델 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },

  // 4.9 출항 RL / 리포트 / What-If (8002)
  '/api/report/health': {
    get: { tags: ['Report / What-If(8002)'], summary: '리포트 헬스', responses: ok('상태', { status: 'ok', model_loaded: true }) },
  },
  '/api/report/generate': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: '리포트 생성',
      description: '출항 의사결정 PDF 리포트 생성(비동기). 상태는 /api/report/status/{job_id}, 다운로드는 /api/report/download/{job_id}.',
      requestBody: jsonBody({ route: 'NSR', ice_class: 'PC6', departure_date_start: '2026-06-10', forecast_days: 14, transit_days: 10 }),
      responses: accepted({ job_id: 'rpt-20260602-7f3a', message: 'report generation started' }),
    },
  },
  '/api/report/status/{job_id}': {
    get: {
      tags: ['Report / What-If(8002)'],
      summary: '리포트 상태',
      parameters: [jobIdPath],
      responses: {
        ...ok('상태', { status: 'completed', progress: 1.0, pdf_path: '/api/report/download/rpt-20260602-7f3a', error: null }),
        404: { $ref: '#/components/responses/NotFound' },
      },
    },
  },
  '/api/report/download/{job_id}': {
    get: {
      tags: ['Report / What-If(8002)'],
      summary: '리포트 PDF 다운로드',
      description: '생성된 PDF 리포트 다운로드(application/pdf, Content-Disposition: attachment). 미완료/미존재 시 404.',
      parameters: [jobIdPath],
      responses: {
        200: { description: 'PDF 바이너리', content: { 'application/pdf': { schema: { type: 'string', format: 'binary' } } } },
        404: { $ref: '#/components/responses/NotFound' },
      },
    },
  },
  '/api/report/rl/train': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: '출항 RL 학습',
      description: '출항 RL 학습 시작. 학습 중 재요청 시 409.',
      requestBody: jsonBody({ timesteps: 300000, route: 'NSR', ice_class: 'PC6' }),
      responses: { ...ok('시작됨', { status: 'started', job_id: 'rltr-001' }), 409: { $ref: '#/components/responses/Conflict' } },
    },
  },
  '/api/report/rl/train-status/{job_id}': {
    get: { tags: ['Report / What-If(8002)'], summary: '출항 RL 학습 상태', parameters: [jobIdPath], responses: ok('상태', { status: 'running', progress: 0.4, job_id: 'rltr-001' }) },
  },
  '/api/report/rl/status': {
    get: { tags: ['Report / What-If(8002)'], summary: '출항 RL 현재 상태', responses: ok('상태', { status: 'idle' }) },
  },
  '/api/report/rl/stop': {
    post: { tags: ['Report / What-If(8002)'], summary: '출항 RL 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },
  '/api/report/rl/calibrate': {
    post: { tags: ['Report / What-If(8002)'], summary: '출항 RL 보정(calibrate)', responses: ok('보정 완료', { status: 'calibrated' }) },
  },
  '/api/report/rl/model-info': {
    get: { tags: ['Report / What-If(8002)'], summary: '출항 RL 모델 정보', responses: ok('모델 정보', { model_loaded: true, version: 'departure-v3', trained_at: '2026-05-30T02:11:00Z' }) },
  },
  '/api/report/rl/departure/train/iterative': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: '출항 RL 반복 학습 시작',
      requestBody: jsonBody({ max_iterations: 8, target_success_rate: 0.92, eval_episodes: 40 }),
      responses: ok('시작됨', { status: 'started' }),
    },
  },
  '/api/report/rl/departure/train/iterative/status': {
    get: { tags: ['Report / What-If(8002)'], summary: '출항 RL 반복 학습 상태', responses: ok('상태', { status: 'running', iteration: 2, max_iterations: 8, best_success_rate: 0.81 }) },
  },
  '/api/report/rl/departure/train/iterative/stop': {
    post: { tags: ['Report / What-If(8002)'], summary: '출항 RL 반복 학습 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },
  '/api/report/rl/multi/train': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: '출항 멀티모델 학습',
      requestBody: jsonBody({ routes: ['NSR', 'NWP'], ice_classes: ['PC6', 'PC7'], timesteps_per_model: 200000 }),
      responses: ok('시작됨', { status: 'started', total_models: 4 }),
    },
  },
  '/api/report/rl/multi/status': {
    get: { tags: ['Report / What-If(8002)'], summary: '출항 멀티모델 상태', responses: ok('상태', { status: 'running', total_models: 4, completed_models: 1 }) },
  },
  '/api/report/rl/multi/stop': {
    post: { tags: ['Report / What-If(8002)'], summary: '출항 멀티모델 중지', responses: ok('중지됨', { status: 'stopped' }) },
  },
  '/api/report/whatif': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: 'What-If 분석 실행',
      description: '출항 조건 변화에 따른 What-If 시나리오 분석(비동기, AI tool-calling). 결과는 /api/report/whatif/status/{job_id}로 폴링.',
      requestBody: jsonBody({ route: 'NSR', ice_class: 'PC6', departure_date_start: '2026-06-10', forecast_days: 14 }),
      responses: accepted({ job_id: 'wif-20260602-22b1' }),
    },
  },
  '/api/report/whatif/status/{job_id}': {
    get: {
      tags: ['Report / What-If(8002)'],
      summary: 'What-If 상태/결과',
      parameters: [jobIdPath],
      responses: ok('상태 및 결과', {
        status: 'completed', progress: 1.0,
        result: {
          scenarios: [{ departure_offset_days: 0, transit_days: 10.2, risk_score: 0.34 }, { departure_offset_days: 7, transit_days: 9.1, risk_score: 0.22 }],
          comparison_text: '7일 지연 출항 시 통항일수 1.1일 단축, 위험도 35% 감소',
          ai_recommendation: '2026-06-17 출항 권장', tool_calls_count: 6,
        },
      }),
    },
  },
  '/api/report/whatif/stats': {
    get: {
      tags: ['Report / What-If(8002)'],
      summary: 'What-If 통계',
      responses: ok('누적 통계', { n_runs: 52, avg_iterations: 3.4, avg_scenarios: 4.1, avg_latency_ms: 8200, by_route: { NSR: 31, NWP: 21 }, by_ice_class: { PC6: 28, PC7: 24 } }),
    },
  },
  '/api/report/sar/train': {
    post: {
      tags: ['Report / What-If(8002)'],
      summary: 'SAR 학습(8005 프록시)',
      description: 'report-service가 sar-server(8005)로 재프록시.',
      requestBody: jsonBody({ epochs: 50, batch_size: 16, synthetic_count: 2000, device: 'cuda' }),
      responses: ok('시작됨', { status: 'started', mode: 'single' }),
    },
  },
  '/api/report/sar/train-status': {
    get: { tags: ['Report / What-If(8002)'], summary: 'SAR 학습 상태(8005 프록시)', responses: ok('상태', { is_training: true, epoch: 22, total_epochs: 50 }) },
  },
  '/api/report/sar/model-info': {
    get: {
      tags: ['Report / What-If(8002)'],
      summary: 'SAR 모델 정보(8005 프록시)',
      responses: ok('모델 정보', { is_training: false, mode: 'iterative', iteration: 4, metrics: { mAP50: 0.84, mAP50_95: 0.61, precision: 0.88, recall: 0.79 } }),
    },
  },

  // 4.10 연료 예측 (8003)
  '/api/fuel/health': {
    get: { tags: ['Fuel — ML(8003)'], summary: '연료 예측 헬스', responses: ok('상태/지표', { status: 'ok', model_loaded: true, metrics: { rmse: 0.031, r2: 0.94, mae: 0.022 } }) },
  },
  '/api/fuel/predict': {
    post: {
      tags: ['Fuel — ML(8003)'],
      summary: '연료 예측',
      description: '선박 제원·해빙 조건 기반 단위거리(NM)당 연료 소모 예측(XGBoost).',
      requestBody: jsonBody({ displacement: 85000, draft: 11.5, engine_power: 21000, ice_thickness: 1.2, ice_concentration: 0.6, ice_class_code: 6 }, '#/components/schemas/FuelInput'),
      responses: ok('예측 결과', { fuel_per_nm: 0.42, unit: 'tons/nm' }),
    },
  },
  '/api/fuel/compare': {
    post: {
      tags: ['Fuel — ML(8003)'],
      summary: 'NSR vs Suez 비교',
      description: '북극항로(NSR)와 수에즈(Suez) 항로의 연료·비용·시간 비교.',
      requestBody: jsonBody({
        displacement: 85000, draft: 11.5, engine_power: 21000, ice_thickness: 1.2, ice_concentration: 0.6, ice_class_code: 6,
        vessel_type: 'container', speed_knots: 14, nsr_distance_nm: 7200, suez_distance_nm: 11500,
      }),
      responses: ok('비교 결과', {
        nsr: { fuel_tons: 3024, cost_usd: 1814400, days: 21.4 },
        suez: { fuel_tons: 3450, cost_usd: 2070000, days: 34.2 },
        comparison: { cost_saving_usd: 255600, cost_saving_percent: 12.3, time_saving_days: 12.8, fuel_saving_tons: 426, nsr_is_cheaper: true },
      }),
    },
  },
  '/api/ml/fuel/train': {
    post: {
      tags: ['Fuel — ML(8003)'],
      summary: '연료 모델 학습',
      requestBody: jsonBody({ n_estimators: 600, max_depth: 8, learning_rate: 0.05 }),
      responses: ok('시작됨', { status: 'started' }),
    },
  },
  '/api/ml/fuel/status': {
    get: { tags: ['Fuel — ML(8003)'], summary: '연료 모델 학습 상태', responses: ok('상태', { status: 'running', progress: 0.5 }) },
  },
  '/api/ml/fuel/result': {
    get: { tags: ['Fuel — ML(8003)'], summary: '연료 모델 학습 결과', responses: ok('결과/지표', { status: 'completed', metrics: { rmse: 0.029, r2: 0.95, mae: 0.021 }, feature_importance: { ice_thickness: 0.41, displacement: 0.22 } }) },
  },
  '/api/ml/whatif/run': {
    post: {
      tags: ['Fuel — ML(8003)'],
      summary: 'ML What-If 실행',
      requestBody: jsonBody({ feature: 'ice_thickness', values: [1.0, 1.8] }),
      responses: ok('시나리오', { scenarios: [{ ice_thickness: 1.0, fuel_per_nm: 0.38 }, { ice_thickness: 1.8, fuel_per_nm: 0.51 }] }),
    },
  },
  '/api/ml/status': {
    get: { tags: ['Fuel — ML(8003)'], summary: 'ML 파이프라인 전체 상태', responses: ok('상태', { status: 'idle', model_loaded: true }) },
  },

  // 4.11 빙산탐지 SAR (8005)
  '/api/sar/train': {
    post: {
      tags: ['SAR — 빙산탐지(8005)'],
      summary: 'SAR 학습',
      description: 'YOLOv8n 빙산 탐지 모델 학습 시작. 학습 중 재요청 시 409.',
      requestBody: jsonBody({ epochs: 50, batch_size: 16, synthetic_count: 2000, device: 'cuda' }),
      responses: { ...ok('시작됨', { status: 'started', mode: 'single' }), 409: { $ref: '#/components/responses/Conflict' } },
    },
  },
  '/api/sar/status': {
    get: { tags: ['SAR — 빙산탐지(8005)'], summary: 'SAR 학습 상태', responses: ok('상태', { is_training: true, mode: 'single', epoch: 22, total_epochs: 50 }) },
  },
  '/api/sar/iterative/status': {
    get: { tags: ['SAR — 빙산탐지(8005)'], summary: 'SAR 반복 학습 상태', responses: ok('상태', { is_training: true, mode: 'iterative', iteration: 3 }) },
  },
  '/api/sar/model-info': {
    get: {
      tags: ['SAR — 빙산탐지(8005)'],
      summary: 'SAR 모델 정보/탐지',
      responses: ok('모델 정보 및 탐지 결과', {
        is_training: false, mode: 'iterative', iteration: 4,
        metrics: { mAP50: 0.84, mAP50_95: 0.61, precision: 0.88, recall: 0.79 },
        detections: [{ id: 'SAR-0007', lat: 78.91, lon: 21.44, length_m: 260, confidence: 0.88 }],
      }),
    },
  },

  // 4.12 프록시
  '/proxy/nsidc': {
    get: {
      tags: ['Proxy'],
      summary: 'NSIDC 타일 프록시',
      description: 'NSIDC WMS/WMTS 타일 프록시. `url` 화이트리스트 검증 후 중계.',
      parameters: [{ name: 'url', in: 'query', required: true, schema: { type: 'string' }, description: '프록시할 NSIDC 원본 URL(URL 인코딩)' }],
      responses: { 200: { description: '원본 리소스(이미지/타일) 바이트스트림 패스스루', content: { 'image/png': { schema: { type: 'string', format: 'binary' } } } } },
    },
  },
  '/proxy/copernicus': {
    get: {
      tags: ['Proxy'],
      summary: 'Copernicus 타일 프록시',
      description: 'Copernicus WMS/WMTS 타일 프록시.',
      parameters: [{ name: 'url', in: 'query', required: true, schema: { type: 'string' }, description: '프록시할 Copernicus 원본 URL(URL 인코딩)' }],
      responses: { 200: { description: '원본 리소스 바이트스트림 패스스루', content: { 'image/png': { schema: { type: 'string', format: 'binary' } } } } },
    },
  },
  '/nsidc-proxy/': {
    get: { tags: ['Proxy'], summary: '(레거시) NSIDC 프록시', deprecated: true, description: 'Deprecated → /proxy/nsidc 사용 권장.', responses: { 200: { description: '패스스루' } } },
  },
  '/cop-proxy/': {
    get: { tags: ['Proxy'], summary: '(레거시) Copernicus 프록시', deprecated: true, description: 'Deprecated → /proxy/copernicus 사용 권장.', responses: { 200: { description: '패스스루' } } },
  },
  '/sentinel-proxy/': {
    get: { tags: ['Proxy'], summary: '(레거시) Sentinel 프록시', deprecated: true, description: 'Deprecated.', responses: { 200: { description: '패스스루' } } },
  },
};

const openapiSpec = {
  openapi: '3.0.3',
  info: {
    title: 'Arctic Digital Twin — API',
    version: '1.0.0',
    description:
      'AI 기반 북극항로 디지털 트윈 플랫폼 API.\n\n' +
      '외부 클라이언트는 오직 Node Gateway(8000)의 `/api/*` 및 `/proxy/*` 경로만 호출한다. ' +
      '내부 마이크로서비스(8001~8005)는 게이트웨이를 통해 프록시되며 외부에 직접 노출되지 않는다.\n\n' +
      '- 비동기 작업(RL 학습/리포트/What-If/SAR)은 `202`로 `job_id`를 반환하고 status 폴링으로 진행률을 조회한다.\n' +
      '- 읽기 조회는 PostgreSQL(Neon) 우선 + JSON 폴백이며 응답 `source` 필드로 출처를 명시한다.\n' +
      '- 본 v1.0은 공개 데모 단계로 인증 토큰을 요구하지 않는다.',
    contact: { name: 'Arctic Digital Twin', url: 'https://arctictwin.com' },
  },
  servers: SERVERS,
  tags: [
    { name: 'System / Health', description: '게이트웨이/마이크로서비스 헬스' },
    { name: 'Ice / Iceberg / Weather', description: '해빙·빙산·기상 데이터' },
    { name: 'Sentinel-1', description: '위성 SAR 제품 카탈로그' },
    { name: 'Route', description: '항로 평가·편집·회피 로그' },
    { name: 'Simulation', description: '시뮬레이션 시나리오' },
    { name: 'Collab / SAR', description: '협업 SAR 탐지 빙산' },
    { name: 'AI Decision', description: '통합 AI 의사결정(병렬 호출 + graceful degradation)' },
    { name: 'RL — 빙산회피(8001)', description: '빙산 회피 SAC 강화학습' },
    { name: 'Report / What-If(8002)', description: '출항 RL · PDF 리포트 · What-If' },
    { name: 'Fuel — ML(8003)', description: '연료 예측 XGBoost' },
    { name: 'SAR — 빙산탐지(8005)', description: '빙산 탐지 YOLOv8n' },
    { name: 'Proxy', description: '외부 WMS/WMTS 타일 프록시' },
  ],
  components,
  paths,
};

module.exports = openapiSpec;
