# sky.py — 天气
# 拉米兰当天天气，一小段文字，让那只蟹知道外面有个"今天"。
# 用 Open-Meteo 公共 API，免费、无需 key。

import requests

LAT, LON = 45.4642, 9.1900   # Milano
API = "https://api.open-meteo.com/v1/forecast"

# WMO weather code → 中文
WMO = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "细密的雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨夹冰雹", 99: "强雷雨夹冰雹",
}


def fetch_sky():
    """返回一句话的天气描述；失败返回空串（管道照常跑）。"""
    try:
        r = requests.get(
            API,
            params={
                "latitude": LAT,
                "longitude": LON,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "Europe/Rome",
                "forecast_days": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        cur = d.get("current", {})
        daily = d.get("daily", {})
        desc = WMO.get(cur.get("weather_code"), "说不清的天")
        t = cur.get("temperature_2m")
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        wind = cur.get("wind_speed_10m")
        parts = [f"米兰此刻{desc}"]
        if t is not None:
            parts.append(f"{t}°C")
        if tmin is not None and tmax is not None:
            parts.append(f"今天 {tmin}°C 到 {tmax}°C")
        if wind is not None and wind >= 20:
            parts.append(f"风大，{wind} km/h")
        return "外面的天气：" + "，".join(parts) + "。"
    except Exception as e:
        print(f"[sky] 拉天气失败，跳过: {e}")
        return ""


if __name__ == "__main__":
    print(fetch_sky())
