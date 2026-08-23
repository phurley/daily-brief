#!/usr/bin/env python3
"""Fetch current conditions and the daily forecast from Open-Meteo and write
weather.json (repo root) conforming to schemas/weather.schema.json."""

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOCATION = {
    "name": "Canton",
    "region": "Michigan",
    "latitude": 42.3086,
    "longitude": -83.4824,
    "timeZone": "America/Detroit",
}

API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LOCATION['latitude']}&longitude={LOCATION['longitude']}"
    "&current=temperature_2m,apparent_temperature,precipitation,weather_code,"
    "relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,sunrise,sunset"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    f"&timezone={LOCATION['timeZone'].replace('/', '%2F')}&forecast_days=7"
)

# WMO weather interpretation codes -> (text, icon)
WMO_CODES = {
    0: ("Clear", "☀"),
    1: ("Mostly clear", "🌤"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁"),
    45: ("Fog", "🌫"),
    48: ("Icy fog", "🌫"),
    51: ("Light drizzle", "🌦"),
    53: ("Drizzle", "🌦"),
    55: ("Heavy drizzle", "🌧"),
    56: ("Light freezing drizzle", "🌧"),
    57: ("Freezing drizzle", "🌧"),
    61: ("Light rain", "🌦"),
    63: ("Rain", "🌧"),
    65: ("Heavy rain", "🌧"),
    66: ("Light freezing rain", "🌧"),
    67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"),
    73: ("Snow", "🌨"),
    75: ("Heavy snow", "❄"),
    77: ("Snow grains", "🌨"),
    80: ("Light rain showers", "🌦"),
    81: ("Rain showers", "🌧"),
    82: ("Heavy rain showers", "🌧"),
    85: ("Snow showers", "🌨"),
    86: ("Heavy snow showers", "❄"),
    95: ("Thunderstorms", "⛈"),
    96: ("Storms with light hail", "⛈"),
    99: ("Storms with heavy hail", "⛈"),
}

COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def condition(code):
    text, icon = WMO_CODES.get(code, ("Unsettled", "🌡"))
    return int(code), text, icon


def compass_direction(degrees):
    return COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]


def with_offset(local_iso, utc_offset_seconds):
    """Open-Meteo returns local times without an offset; make them RFC 3339."""
    minutes = abs(utc_offset_seconds) // 60
    sign = "-" if utc_offset_seconds < 0 else "+"
    stamp = local_iso if len(local_iso) > 16 else local_iso + ":00"
    return f"{stamp}{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def build(payload, now_iso):
    offset = payload["utc_offset_seconds"]
    cur = payload["current"]
    code, text, icon = condition(cur["weather_code"])
    wind = {
        "speedMph": cur["wind_speed_10m"],
        "directionDegrees": cur["wind_direction_10m"] % 360,
        "direction": compass_direction(cur["wind_direction_10m"]),
    }
    if cur.get("wind_gusts_10m") is not None:
        wind["gustMph"] = cur["wind_gusts_10m"]

    daily = []
    d = payload["daily"]
    for i, date in enumerate(d["time"]):
        dcode, dtext, dicon = condition(d["weather_code"][i])
        chance = d["precipitation_probability_max"][i]
        chance = int(chance) if chance is not None else 0
        high, low = d["temperature_2m_max"][i], d["temperature_2m_min"][i]
        narrative = (
            f"{dtext} with a high near {round(high)}°F and a low around "
            f"{round(low)}°F; {chance}% chance of precipitation."
        )
        daily.append({
            "date": date,
            "highF": high,
            "lowF": low,
            "conditionCode": dcode,
            "conditionText": dtext,
            "icon": dicon,
            "precipitationChancePercent": chance,
            "sunrise": with_offset(d["sunrise"][i], offset),
            "sunset": with_offset(d["sunset"][i], offset),
            "narrative": narrative,
        })

    return {
        "schemaVersion": "1.0.0",
        "generatedAt": now_iso,
        "location": LOCATION,
        "current": {
            "observedAt": with_offset(cur["time"], offset),
            "temperatureF": cur["temperature_2m"],
            "feelsLikeF": cur["apparent_temperature"],
            "conditionCode": code,
            "conditionText": text,
            "icon": icon,
            "precipitationIn": max(cur["precipitation"], 0),
            "humidityPercent": int(cur["relative_humidity_2m"]),
            "wind": wind,
        },
        "daily": daily,
        "sources": [
            {
                "name": "Open-Meteo",
                "url": API_URL,
                "retrievedAt": now_iso,
            }
        ],
    }


def main():
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with urllib.request.urlopen(API_URL, timeout=30) as response:
        payload = json.load(response)

    weather = build(payload, now_iso)
    out_path = Path(__file__).resolve().parent.parent / "weather.json"
    out_path.write_text(json.dumps(weather, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {out_path} ({len(weather['daily'])} forecast days)")


if __name__ == "__main__":
    sys.exit(main())
