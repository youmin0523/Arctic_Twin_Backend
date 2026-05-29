# 북극항로(NSR) 마스터 라우팅 알고리즘 통합 명세서

본 문서는 디지털 트윈 기반 북극항로 가상 검증 플랫폼의 핵심인 '운항 가능 여부 판별 및 우회 경로 산출 알고리즘'의 명세와 Python 소스 코드를 정의합니다. 한국선급(KR) Polar Code 기준, POLARIS 방법론, 그리고 상용 항해에 필요한 실무 기상/규제 필터를 모두 포함합니다.

---

## 1. 알고리즘 판단 로직 (Decision Tree)

본 알고리즘은 다음 5단계의 순차적 필터링을 거쳐 선박의 북극항로 통과 여부를 결정합니다.

1. **지정학 및 환경 규제 필터:** 대러 제재, NSRA 사전 허가, PWOM 비치 여부, **HFO(중질유) 사용 금지 규정** 검증
2. **물리적 크기 필터:** 북극항로 수심 한계(흘수 12.5m) 및 쇄빙선 수로 한계(선폭 35m) 검증
3. **Polar Code 생존 및 통신 필터:** 생존 보장 시간(5일), 설계 온도 여유치, 필수 설비 4종, **고위도(북위 75도 이상) 진입 시 LEO(저궤도) 통신망** 검증
4. **선종별 특화 기상 필터:** - 컨테이너선: 파고 한계점 및 기온 강하에 따른 치명적 선체 착빙(Icing) 한계점 검증
   - LNG선/쇄빙선: 파고 경고 및 BOG(기화 가스) 최소화를 위한 주의 운항 판별
   - 공통: 가시거리 1km 미만 시 감속 페널티 부여
5. **POLARIS 빙해역 위험 지수(RIO) 평가:** 자체 모듈을 통해 빙상 데이터를 RIO 점수로 환산하여 최종 승인 여부 결정

---

## 2. Master Routing Algorithm (Python Source Code)

```python
def calculate_rio(ice_class, ice_conditions):
    """
    1. POLARIS 방법론에 따른 RIO(Risk Index Outcome) 산출 모듈
    수식: RIO = Sum(각 얼음 종류별 농도(10분위수) * 해당 RIV 값)
    """

    # RIV(Risk Index Value) 룩업 테이블 (내빙 등급 및 빙질별 가상 예시 포함)
    riv_table = {
        'PC5': {
            'Thin First-Year': 2,
            'Medium First-Year': 1,
            'Thick First-Year': 0,
            'Old Ice': -1
        },
        'PC7': {
            'Thin First-Year': 1,
            'Medium First-Year': -1,
            'Thick First-Year': -2,
            'Old Ice': -3
        },
        'None': {
            'Thin First-Year': -1,
            'Medium First-Year': -2,
            'Thick First-Year': -3,
            'Old Ice': -4
        }
    }

    # 선박의 내빙 등급이 테이블에 없으면 가장 보수적인 'None' 등급 적용
    target_rivs = riv_table.get(ice_class, riv_table['None'])
    rio_score = 0.0

    for ice in ice_conditions:
        ice_type = ice.get('type')
        # 농도 값(0.1~1.0)에 10을 곱하여 1~10분위수 정수로 환산
        concentration = int(ice.get('concentration_tenths', 0.0) * 10)

        # 테이블에 없는 빙질이 들어올 경우 강제 위험 처리(-3)
        riv_value = target_rivs.get(ice_type, -3)

        # RIO 누적 계산
        rio_score += concentration * riv_value

    return float(rio_score)


def evaluate_routing(ship_data):
    """
    2. 전체 라우팅 판단 로직 (Decision Tree - 순차적 검증)
    """

    # --- Step 1: 지정학 및 행정/환경 규제 필터 ---
    if ship_data.get('is_sanctioned_country', False):
        return {'status': 'REROUTE_CAPE', 'reason': '제재 위반 리스크로 희망봉 우회'}
    if not ship_data.get('has_nsra_permit', False) or not ship_data.get('has_pwom', False):
        return {'status': 'REROUTE_SUEZ', 'reason': 'NSRA 당국 사전 허가 미취득 또는 PWOM 문서 미비'}

    fuel_type = ship_data.get('fuel_type', 'MGO')
    has_hfo_exemption = ship_data.get('has_hfo_exemption', False)
    if fuel_type == 'HFO' and not has_hfo_exemption:
        return {'status': 'REROUTE_SUEZ', 'reason': 'IMO 북극해 HFO(중질유) 사용 및 적재 금지 규정 위반'}

    # --- Step 2: 물리적 크기 필터 ---
    if ship_data.get('draft', 0.0) > 12.5:
        return {'status': 'REROUTE_SUEZ', 'reason': '북극항로 수심 제한(12.5m) 초과'}
    if ship_data.get('beam', 0.0) > 35.0:
        return {'status': 'REROUTE_SUEZ', 'reason': '선폭 과다(35m 초과)로 단독/에스코트 통과 불가'}

    # --- Step 3: Polar Code 생존/설비/통신 기준 필터 ---
    if ship_data.get('max_rescue_days_capacity', 0) < 5:
        return {'status': 'REROUTE_SUEZ', 'reason': '최소 생존 보장 시간(5일) 미달'}

    is_temp_below = ship_data.get('is_temp_below_minus_10', False)
    if is_temp_below and ship_data.get('design_temp_margin', 0.0) < 10:
        return {'status': 'REROUTE_SUEZ', 'reason': '극지 설계 온도 10도 여유분 미확보'}

    required_equipments = [
        ship_data.get('has_winterization', False),
        ship_data.get('has_zero_discharge', False),
        ship_data.get('has_polar_comms', False),
        ship_data.get('has_ice_navigator', False)
    ]
    if not all(required_equipments):
        return {'status': 'REROUTE_SUEZ', 'reason': 'Polar Code 필수 설비 및 인력 미달'}

    latitude = ship_data.get('latitude', 70.0)
    comms_type = ship_data.get('comms_type', 'GEO')
    if latitude >= 75.0 and comms_type != 'LEO':
        return {'status': 'REROUTE_SUEZ', 'reason': '북위 75도 이상 고위도 진입 시 LEO(저궤도) 통신 장비 필수 (현재 GEO 보유)'}

    # --- Step 4: 선종 및 특수 기상 필터 ---
    ship_type = ship_data.get('ship_type', 'General')
    wave_height = ship_data.get('wave_height', 0.0)
    visibility_km = ship_data.get('visibility_km', 10.0)
    weather_warning = ""

    if ship_type == 'Container Ship':
        if wave_height > 4.0:
            return {'status': 'REROUTE_SUEZ', 'reason': f'컨테이너선 한계 파고({wave_height}m) 초과. 화물 유실 위험'}
        if is_temp_below and wave_height > 2.5:
            return {'status': 'REROUTE_SUEZ', 'reason': '컨테이너선 기상 경고: 영하 기온 및 높은 파고로 인한 치명적 선체 착빙(Vessel Icing) 예상'}

    elif ship_type == 'LNG Carrier' and wave_height > 6.0:
        weather_warning += '[LNG선 경고: BOG 최소화를 위한 감속] '

    elif ship_type == 'Icebreaker' and wave_height > 8.0:
        weather_warning += '[쇄빙선 경고: 황천 해역 호송 임무 주의] '

    if visibility_km < 1.0:
        weather_warning += f'[가시거리 경고: {visibility_km}km. 해무/극야로 인한 속도 50% 감속 요망] '

    weather_warning = weather_warning.strip()

    # --- Step 5: POLARIS 빙해역 위험 지수(RIO) 평가 ---
    ice_class = ship_data.get('ice_class', 'None')
    ice_conditions = ship_data.get('ice_conditions', [])
    rio_score = calculate_rio(ice_class, ice_conditions)

    if rio_score < -10:
        return {'status': 'REROUTE_SUEZ', 'reason': f'POLARIS RIO 심각한 위험({rio_score}점). 특별 고려 대상 해역으로 항해 불가'}
    elif -10 <= rio_score < 0:
        base_reason = f'POLARIS 고위험 해역({rio_score}점). 에스코트 필수.'
        if weather_warning: base_reason += f' | {weather_warning}'
        return {'status': 'NSR_RESTRICTED', 'reason': base_reason}
    else: # rio_score >= 0
        status = 'NSR_RESTRICTED' if weather_warning else 'NSR_APPROVED'
        reason = f'POLARIS 정상({rio_score}점). {weather_warning}'.strip()
        return {'status': status, 'reason': reason}


# ==========================================
# 3. 테스트 및 검증 코드 (Test Cases)
# ==========================================
if __name__ == "__main__":

    # 뼈대가 되는 기본 선박 제원 (에러가 없는 완벽한 상태)
    base_ship_data = {
        'ship_type': 'LNG Carrier', 'draft': 11.0, 'beam': 30.0,
        'latitude': 76.0, 'comms_type': 'LEO', 'fuel_type': 'LNG',
        'wave_height': 1.5, 'visibility_km': 10.0,
        'ice_class': 'PC5', 'has_pwom': True, 'max_rescue_days_capacity': 10,
        'is_temp_below_minus_10': True, 'design_temp_margin': 15,
        'has_winterization': True, 'has_zero_discharge': True,
        'has_polar_comms': True, 'has_ice_navigator': True,
        'is_sanctioned_country': False, 'has_nsra_permit': True
    }

    # Case 1: 완벽한 선박 케이스 (RIO 점수 17점 -> NSR_APPROVED)
    case_1 = base_ship_data.copy()
    case_1['ice_conditions'] = [
        {'type': 'Thin First-Year', 'concentration_tenths': 0.7}, # 7 * 2 = 14
        {'type': 'Medium First-Year', 'concentration_tenths': 0.3} # 3 * 1 = 3 -> Total = 17.0
    ]

    # Case 2: 생존 일수(3일) 미달로 우회하는 케이스 (REROUTE_SUEZ)
    case_2 = base_ship_data.copy()
    case_2['max_rescue_days_capacity'] = 3
    case_2['ice_conditions'] = case_1['ice_conditions']

    # Case 3: 얼음 농도가 너무 짙어 RIO 점수가 -12가 나오는 케이스 (REROUTE_SUEZ)
    case_3 = base_ship_data.copy()
    case_3['ice_class'] = 'PC7'
    case_3['ice_conditions'] = [
        {'type': 'Thick First-Year', 'concentration_tenths': 0.6}, # 6 * (-2) = -12
        {'type': 'Medium First-Year', 'concentration_tenths': 0.0} # Total = -12.0
    ]

    # Case 4: 컨테이너선 파고 및 착빙 위험 (REROUTE_SUEZ)
    case_4 = base_ship_data.copy()
    case_4['ship_type'] = 'Container Ship'
    case_4['wave_height'] = 3.0
    case_4['ice_conditions'] = case_1['ice_conditions']

    # Case 5: 가시거리 제한 페널티 부여 (NSR_RESTRICTED)
    case_5 = base_ship_data.copy()
    case_5['visibility_km'] = 0.5
    case_5['ice_conditions'] = case_1['ice_conditions']

    # 실행 결과 출력
    print("--- 1. 정상 통과 (NSR_APPROVED) ---")
    print(evaluate_routing(case_1))

    print("\n--- 2. 생존 일수 미달 (REROUTE_SUEZ) ---")
    print(evaluate_routing(case_2))

    print("\n--- 3. 빙상 위험 RIO -12 (REROUTE_SUEZ) ---")
    print(evaluate_routing(case_3))

    print("\n--- 4. 컨테이너 착빙 위험 (REROUTE_SUEZ) ---")
    print(evaluate_routing(case_4))

    print("\n--- 5. 가시거리 페널티 (NSR_RESTRICTED) ---")
    print(evaluate_routing(case_5))
```
