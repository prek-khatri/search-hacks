"""Google Routes transit itineraries -> timed segments and sample points."""
import os, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .core import cached_post, decode_polyline, haversine

TZ = ZoneInfo("America/Los_Angeles")
MASK = ",".join(["routes.duration", "routes.legs.steps.travelMode", "routes.legs.steps.staticDuration",
                 "routes.legs.steps.distanceMeters", "routes.legs.steps.polyline.encodedPolyline",
                 "routes.legs.steps.startLocation", "routes.legs.steps.endLocation", "routes.legs.steps.transitDetails",
                 "routes.legs.steps.navigationInstruction.instructions"])


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def _secs(s):
    return int(str(s).rstrip("s") or 0)


def _ll(loc):
    ll = (loc or {}).get("latLng", {})
    return ll.get("latitude"), ll.get("longitude")


def fetch_routes(origin, destination, depart_local, transit_preferences=None, travel_mode="TRANSIT"):
    body = {"origin": {"address": origin}, "destination": {"address": destination}, "travelMode": travel_mode,
            "departureTime": depart_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "computeAlternativeRoutes": True}
    if transit_preferences:
        body["transitPreferences"] = transit_preferences
    data, _ = cached_post("routes", "https://routes.googleapis.com/directions/v2:computeRoutes", body,
                          {"X-Goog-Api-Key": os.environ["GOOGLE_MAPS_API_KEY"], "X-Goog-FieldMask": MASK})
    return data.get("routes", [])


def build_itinerary(route, not_before):
    """Turn one Google route into segments: walk / wait / ride, each with start/end local times.

    Google already times the start so the first wait is ~0; we keep its implied start,
    but never earlier than `not_before` (the event end)."""
    steps = [s for leg in route["legs"] for s in leg["steps"]]
    # merge consecutive walking steps
    groups = []
    for s in steps:
        if s.get("transitDetails"):
            groups.append(("ride", s))
        elif groups and groups[-1][0] == "walk":
            groups[-1][1].append(s)
        else:
            groups.append(("walk", [s]))

    rides = [g[1] for g in groups if g[0] == "ride"]
    if rides:
        first_walk = sum(_secs(s["staticDuration"]) for s in (groups[0][1] if groups[0][0] == "walk" else []))
        first_dep = _t(rides[0]["transitDetails"]["stopDetails"]["departureTime"])
        leave = max(not_before, first_dep - timedelta(seconds=first_walk))
    else:
        leave = not_before

    segs, clock = [], leave
    for i, (kind, val) in enumerate(groups):
        if kind == "walk":
            secs = sum(_secs(s["staticDuration"]) for s in val)
            pts = []
            for s in val:
                enc = (s.get("polyline") or {}).get("encodedPolyline")
                if enc:
                    pts.extend(decode_polyline(enc))
            dist = sum(s.get("distanceMeters", 0) for s in val)
            segs.append({"kind": "walk", "start": clock, "end": clock + timedelta(seconds=secs),
                         "meters": dist, "points": pts})
            clock += timedelta(seconds=secs)
        else:
            sd = val["transitDetails"]["stopDetails"]
            line = val["transitDetails"].get("transitLine", {})
            dep, arr = _t(sd["departureTime"]), _t(sd["arrivalTime"])
            stop = sd["departureStop"]
            lat, lng = _ll(stop.get("location"))
            if dep > clock:
                segs.append({"kind": "wait", "start": clock, "end": dep, "stop": stop.get("name"),
                             "lat": lat, "lng": lng})
            enc = (val.get("polyline") or {}).get("encodedPolyline")
            segs.append({"kind": "ride", "start": dep, "end": arr, "points": decode_polyline(enc) if enc else [],
                         "line": line.get("nameShort") or line.get("name"),
                         "vehicle": (line.get("vehicle") or {}).get("type"),
                         "from": stop.get("name"), "to": sd["arrivalStop"].get("name")})
            clock = arr
    streets = []
    for st in steps:
        txt = ((st.get("navigationInstruction") or {}).get("instructions") or "").split("\n")[0]
        m = re.search(r"\b(?:onto|on|toward)\s+(.+?)(?:\s+Pass by|\s+Destination|$)", txt)
        if m and m.group(1) not in streets:
            streets.append(m.group(1).strip())
    return {"leave": leave, "arrive": clock, "segments": segs, "streets": streets}


def sample_points(itin, step_m=80):
    """Points the user occupies, each with a clock time and the seconds it stands for."""
    out = []
    for seg in itin["segments"]:
        secs = (seg["end"] - seg["start"]).total_seconds()
        if seg["kind"] == "wait" and seg["lat"] is not None:
            n = max(1, int(round(secs / 60)))
            for k in range(n):
                out.append({"t": seg["start"] + timedelta(seconds=secs * k / n), "lat": seg["lat"], "lng": seg["lng"],
                            "secs": secs / n, "kind": "wait", "where": seg["stop"]})
        elif seg["kind"] == "walk" and seg["points"] and secs > 0:
            pts = seg["points"]
            cum = [0.0]
            for a, b in zip(pts, pts[1:]):
                cum.append(cum[-1] + haversine(*a, *b))
            total = cum[-1] or 1
            n = max(1, int(total // step_m))
            for k in range(n):
                target = total * (k + 0.5) / n
                j = next((i for i, c in enumerate(cum) if c >= target), len(pts) - 1)
                lat, lng = pts[j]
                out.append({"t": seg["start"] + timedelta(seconds=secs * target / total), "lat": lat, "lng": lng,
                            "secs": secs / n, "kind": "walk", "where": None})
    return out


def public(itin):
    """JSON-safe itinerary without raw polylines."""
    segs = []
    for s in itin["segments"]:
        d = {k: v for k, v in s.items() if k != "points"}
        d["start"], d["end"] = s["start"].strftime("%-I:%M %p"), s["end"].strftime("%-I:%M %p")
        d["minutes"] = round((s["end"] - s["start"]).total_seconds() / 60, 1)
        segs.append(d)
    return segs
