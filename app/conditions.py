"""Hourly forecast at the start of the trip: temperature, rain chance, air quality, daylight (Open-Meteo, no key)."""
from .core import cached_get


def conditions(lat, lng, when):
    day = when.strftime("%Y-%m-%d")
    hour = when.strftime("%Y-%m-%dT%H:00")
    out = {"source": "Open-Meteo forecast", "hour": when.strftime("%-I %p")}
    try:
        w = cached_get("meteo", f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}"
                       f"&hourly=temperature_2m,precipitation_probability,is_day&timezone=America/Los_Angeles"
                       f"&start_date={day}&end_date={day}")["hourly"]
        i = w["time"].index(hour)
        out.update(temp_c=w["temperature_2m"][i], rain_pct=w["precipitation_probability"][i], daylight=bool(w["is_day"][i]))
    except Exception:
        out["weather"] = "no information"
    try:
        a = cached_get("meteo-aq", f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lng}"
                       f"&hourly=us_aqi&timezone=America/Los_Angeles&start_date={day}&end_date={day}")["hourly"]
        out["aqi"] = a["us_aqi"][a["time"].index(hour)]
    except Exception:
        out["aqi"] = None
    return out


def air_outside(points):
    """AQI while you are outside on this route: every walking/waiting minute, at the grid cell and hour
    you are actually there (Open-Meteo air quality, ~0.1 degree grid). Time on a bus or train is not counted."""
    total = weighted = 0.0
    worst = None
    hourly = {}
    for p in points:
        cell = (round(p["lat"], 1), round(p["lng"], 1))
        day = p["t"].strftime("%Y-%m-%d")
        key = (cell, day)
        if key not in hourly:
            try:
                h = cached_get("meteo-aq", f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={cell[0]}&longitude={cell[1]}"
                                           f"&hourly=us_aqi&timezone=America/Los_Angeles&start_date={day}&end_date={day}")["hourly"]
                hourly[key] = dict(zip(h["time"], h["us_aqi"]))
            except Exception:
                hourly[key] = {}
        aqi = hourly[key].get(p["t"].strftime("%Y-%m-%dT%H:00"))
        if aqi is None:
            continue
        total += p["secs"]
        weighted += aqi * p["secs"]
        worst = aqi if worst is None else max(worst, aqi)
    if not total:
        return None
    return {"aqi_avg": round(weighted / total), "aqi_max": worst, "minutes_outside": round(total / 60, 1),
            "cells": len({k[0] for k in hourly})}
