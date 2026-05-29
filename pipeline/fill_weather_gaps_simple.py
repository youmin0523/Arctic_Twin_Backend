import json
import os
from datetime import datetime, timezone

def estimate_temp(lat):
    if lat > 70: return round(-12.0 - (lat - 70) * 0.5, 1)
    elif lat > 45: return round(8.0 - (lat - 45) * 0.6, 1)
    return round(22.0 - (lat - 30) * 0.4, 1)

def estimate_vis(lat):
    if lat > 75: return round(3.5 - (lat - 75) * 0.2, 2)
    elif lat > 60: return round(10.0 - (lat - 60) * 0.4, 2)
    return round(18.0 - (lat / 12.0), 2)

json_path = r"c:\Users\Codelab\Desktop\PROJECT\Portfolio\Digital Twin\backend\data\weather_latest.json"

with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

data["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
data["source"] = "Weather API (with Interpolated High-Lat Fallback)"

for route in data.get("routes", {}).values():
    wp_list = route.get("waypoints", [])
    for wp in wp_list:
        lat = wp.get("lat", 0)
        if wp.get("temperature_c") is None:
            wp["temperature_c"] = estimate_temp(lat)
        if wp.get("visibility_km") is None:
            wp["visibility_km"] = estimate_vis(lat)
    
    # Recalculate summary
    temps = [wp["temperature_c"] for wp in wp_list]
    visibs = [wp["visibility_km"] for wp in wp_list]
    route["route_summary"]["min_temperature_c"] = min(temps) if temps else None
    route["route_summary"]["min_visibility_km"] = min(visibs) if visibs else None

with open(json_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("DONE_INTERPOLATION")
