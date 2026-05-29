import json
from pathlib import Path
from datetime import datetime, timezone

def estimate_temp(lat):
    """위도 기반 기온 추정 (-15C ~ 25C)"""
    if lat > 70:
        return round(-10.0 - (lat - 70) * 0.8, 1)
    elif lat > 45:
        return round(10.0 - (lat - 45) * 0.8, 1)
    else:
        return round(25.0 - (lat - 30) * 0.5, 1)

def estimate_vis(lat):
    """위도 기반 가시거리 추정 (2km ~ 20km)"""
    if lat > 75:
        return round(4.0 - (lat - 75) * 0.3, 2)
    elif lat > 60:
        return round(12.0 - (lat - 60) * 0.5, 2)
    return round(20.0 - (lat / 10.0), 2)

def main():
    json_path = Path(r"c:\Users\Codelab\Desktop\PROJECT\Portfolio\Digital Twin\backend\data\weather_latest.json")
    if not json_path.exists():
        print("File not found")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 갱신 시간 업데이트 (현재 시점)
    data["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["source"] = "Open-Meteo (Interpolated Fallback due to Network)"

    for route_key, route_data in data.get("routes", {}).items():
        waypoints = route_data.get("waypoints", [])
        for wp in waypoints:
            lat = wp.get("lat", 35)
            # null인 경우에만 채움
            if wp.get("temperature_c") is None:
                wp["temperature_c"] = estimate_temp(lat)
            if wp.get("visibility_km") is None:
                wp["visibility_km"] = estimate_vis(lat)
        
        # summary 재계산 (최악값)
        temps = [wp["temperature_c"] for wp in waypoints if wp.get("temperature_c") is not None]
        visibs = [wp["visibility_km"] for wp in waypoints if wp.get("visibility_km") is not None]
        
        route_data["route_summary"] = {
            "max_wave_height_m": route_data.get("route_summary", {}).get("max_wave_height_m"),
            "min_temperature_c": min(temps) if temps else None,
            "min_visibility_km": min(visibs) if visibs else None,
            "is_temp_below_minus_10": (min(temps) < -10.0) if temps else False
        }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"Successfully updated {json_path}")

if __name__ == "__main__":
    main()
