"""Build the card: routes -> places -> minutes without an open door -> verification -> options."""
from datetime import datetime, timedelta
from .core import Trace, haversine
from .routes import TZ, fetch_routes, build_itinerary, sample_points, public
from .places import find_places, open_at, hours_text, CATEGORIES
from .metric import anchors_for, evaluate, RADIUS
from .verify import verify
from .conditions import conditions, air_outside


def _city(address):
    parts = [p.strip() for p in address.split(",")]
    return ", ".join(parts[-2:-1] + ["CA"]) if len(parts) >= 3 else address


def plan(origin, destination, end_local: datetime, max_extra=10, categories=None, mode="transit"):
    trace = Trace()
    # Stride searches wider than Google's default answer: later departures within the limit, plus bus-only routes
    # Google's default answer, then every public-transport mode on its own (each surfaces routes the default hides),
    # then later departures within the limit
    searches = [(end_local, None, "Google")]
    if mode == "walk":
        searches += [(end_local + timedelta(minutes=m), None, f"leave +{m} min") for m in (15, 30)]
    else:
      searches += [(end_local, {"allowedTravelModes": [m]}, label) for m, label in
                 (("BUS", "bus routes"), ("TRAIN", "train routes"), ("LIGHT_RAIL", "light rail routes"), ("RAIL", "rail routes"))]
      searches += [(end_local + timedelta(minutes=m), None, f"leave +{m} min") for m in (10, 20, 30) if m <= max_extra]
    itins, seen, from_google = [], set(), 0
    for depart, prefs, source in searches:
        for r in fetch_routes(origin, destination, depart, prefs, "WALK" if mode == "walk" else "TRANSIT"):
            it = build_itinerary(r, end_local)
            sig = (it["leave"], it["arrive"], tuple(s.get("line") for s in it["segments"]))
            if sig not in seen:
                seen.add(sig)
                it["source"] = source
                itins.append(it)
                from_google += source == "Google"
    if not itins:
        return {"error": "No transit routes found for this trip and time."}
    google_first = itins[0]
    soonest = min(it["arrive"] for it in itins)
    rest = sorted([it for it in itins[1:] if it["arrive"] <= soonest + timedelta(minutes=60)], key=lambda it: it["arrive"])
    itins = [google_first] + rest[:11]
    trace.log(f"Routes: {len(itins)} walking options compared (Google's walking routes, leaving now and later)" if mode == "walk" else
              f"Routes: {len(itins)} public-transport options compared ({from_google} from Google's default answer, "
              f"plus bus, train and light-rail searches and later departures)")

    anchors = []
    for it in itins:
        if mode == "walk":
            st = it.get("streets") or []
            found = [origin] + [f"{a} & {b}, {_city(origin)}" for a, b in zip(st, st[1:])] + [destination]
        else:
            found = anchors_for(it, origin, destination, _city(origin), _city(destination))
        for a in found:
            if a not in anchors:
                anchors.append(a)
    anchors = anchors[:10]
    places, nq = find_places(trace, anchors, categories or CATEGORIES)
    trace.log(f"Places: {len(places)} businesses with hours found around {len(anchors)} stops ({nq} map searches through Telem)")
    pmap = {p["key"]: p for p in places}

    for it in itins:
        it["points"] = sample_points(it)
        it["uncovered"], it["cover"] = evaluate(it["points"], places)
    trace.log(f"Walking and waiting: {sum(len(it['points']) for it in itins)} points checked minute by minute")

    # which places does the number depend on? covering places + nearest open door at each wait
    weight, near_at = {}, {}
    for it in itins:
        for p, c in zip(it["points"], it["cover"]):
            for _, k in c[:1]:
                weight[k] = weight.get(k, 0) + p["secs"]
                near_at.setdefault(k, p["t"])
        for w in _waits(it):
            n = _nearest_open(w, places)
            if n:
                weight[n[1]["key"]] = weight.get(n[1]["key"], 0) + 1
                near_at.setdefault(n[1]["key"], w["start"])
    to_check = [pmap[k] for k, _ in sorted(weight.items(), key=lambda x: -x[1])[:5]]
    status = verify(trace, to_check, itins[0]["leave"], near_at) if to_check else {}
    trace.log(f"Hours checked for {len(to_check)} key places: listing + web search + recent reviews")

    for it in itins:
        it["uncovered"], it["cover"] = evaluate(it["points"], places, {k: v["status"] for k, v in status.items()})
        it["wait_inside"] = _wait_inside(it, places, status)
        it["stride_uncovered"] = round(it["uncovered"] - sum(s["minutes_gained"] for s in it["wait_inside"]), 1)

    google = itins[0]
    earliest = min(it["arrive"] for it in itins)
    within = [it for it in itins if it["arrive"] <= earliest + timedelta(minutes=max_extra)]
    best = min(within, key=lambda it: (it["stride_uncovered"], it["arrive"]))
    later = [it for it in itins if it["leave"] > google["leave"] and it is not best]
    first_line = _first_line(google)
    # the backup is the same first bus/train, next departure, when Google gave one; else the later option with least time on foot
    same = [it for it in later if _first_line(it) == first_line]
    backup = min(same, key=lambda it: it["leave"]) if same else (
        min(later, key=lambda it: sum(p["secs"] for p in it["points"])) if later else None)

    # Stride's two options: fewest minutes without an open door across ALL options (trip time may be longer)
    # fair comparison: Stride only picks routes that leave at (about) the same time as Google's
    same_start = [it for it in itins if abs((it["leave"] - google["leave"]).total_seconds()) <= SAME_START_MIN * 60]
    # only routes with strictly fewer (whole) minutes with nothing open nearby than Google's; never Google itself
    better = [it for it in same_start if it is not google and round(it["stride_uncovered"]) < round(google["uncovered"])]
    ranked = sorted(better, key=lambda it: (it["stride_uncovered"], it["arrive"]))
    stride1 = ranked[0] if ranked else None
    stride2 = ranked[1] if len(ranked) > 1 else None
    best = stride1 or google
    options = [{**_option("Google's first pick", google, places, status, earliest), "role": "google"}]
    if stride1 is not None:
        options.append({**_option("Stride option 1", stride1, places, status, earliest, stride=True), "role": "stride",
                        "same_as_google": False})
    if stride2 is not None:
        options.append({**_option("Stride option 2", stride2, places, status, earliest, stride=True), "role": "stride2",
                        "same_as_google": stride2 is google})
    for it in itins:
        if it is not google and it is not stride1 and it is not stride2:
            role = "backup" if it is backup else "other"
            options.append({**_option("Next option if you miss it" if it is backup else "Other option", it, places,
                                      status, earliest), "role": role,
                            "within_limit": it["arrive"] <= earliest + timedelta(minutes=max_extra)})

    notes = []
    if stride1 is None:
        notes.append(f"Google's fastest route already has the least time with nothing open nearby, "
                     f"among the {len(same_start)} options leaving around {google['leave']:%-I:%M %p}.")
    if google["leave"] - end_local > timedelta(minutes=60):
        notes.append(f"No transit leaves before {google['leave']:%-I:%M %p}. "
                     f"That is {int((google['leave'] - end_local).total_seconds() // 60)} minutes after you are ready.")

    first_walk = next((s for s in google["segments"] if s["kind"] == "walk" and s["points"]), None)
    lat, lng = first_walk["points"][0] if first_walk else (37.3874, -122.0398)
    calls = trace.calls
    return {
        "options": options,
        "notes": notes,
        "conditions": conditions(lat, lng, google["leave"]),
        "trace": {"lines": trace.lines, "calls_by_kind": calls, "providers": sorted(trace.providers),
                  "routing": trace.routing,
                  "summary": f"Checked {sum(calls.values())} Telem calls: " +
                             ", ".join(f"{v} {k}" for k, v in calls.items())},
        "radius_m": RADIUS,
    }


SAME_START_MIN = 10
WALK_M_PER_MIN = 80
BUFFER_MIN = 2


def _wait_inside(it, places, status):
    """For waits with nothing open within RADIUS: if the nearest open door is close enough to walk there,
    wait inside, and walk back with BUFFER_MIN to spare, suggest it. Both walks still count as minutes
    without an open door; only the time inside is gained."""
    out = []
    for w in _waits(it):
        n = _nearest_open(w, places, status)
        if not n or n[0] <= RADIUS:
            continue
        wait_min = (w["end"] - w["start"]).total_seconds() / 60
        walk_min = n[0] / WALK_M_PER_MIN
        inside = wait_min - 2 * walk_min - BUFFER_MIN
        if inside >= 1:
            arrive_in = w["start"] + timedelta(minutes=walk_min)
            leave_at = w["end"] - timedelta(minutes=walk_min + BUFFER_MIN)
            out.append({"stop": w["stop"], "place": n[1]["name"], "distance_m": round(n[0]),
                        "inside_from": arrive_in.strftime("%-I:%M %p"), "inside_until": leave_at.strftime("%-I:%M %p"),
                        "walk_min": round(walk_min, 1), "minutes_gained": round(inside, 1),
                        "lat": n[1]["lat"], "lng": n[1]["lng"], "status": status.get(n[1]["key"], {}).get("status", "listed")})
    return out


def _first_line(it):
    return next((s.get("line") for s in it["segments"] if s["kind"] == "ride"), None)


def _covered(lat, lng, when, places, status):
    for p in places:
        if status.get(p["key"], {}).get("status") == "uncertain":
            continue
        if haversine(lat, lng, p["lat"], p["lng"]) <= RADIUS and open_at(p, when) is True:
            return True
    return False


def _map(it, places, status):
    """Geometry for the map: walks split into covered/uncovered runs, rides, waits.

    Colours come from the SAME sample points as the minutes number, so map and number always agree."""
    lines = []
    samples = [(p["t"], bool(c)) for p, c in zip(it["points"], it["cover"]) if p["kind"] == "walk"]
    for s in it["segments"]:
        pts = s.get("points") or []
        if s["kind"] == "ride" and pts:
            lines.append({"kind": "ride", "coords": pts, "label": s.get("line")})
        elif s["kind"] == "walk" and pts:
            secs = (s["end"] - s["start"]).total_seconds()
            mine = [x for x in samples if s["start"] <= x[0] <= s["end"]]
            run, prev = [pts[0]], None
            for i, (lat, lng) in enumerate(pts):
                tv = s["start"] + timedelta(seconds=secs * i / max(1, len(pts) - 1))
                c = min(mine, key=lambda x: abs((x[0] - tv).total_seconds()))[1] if mine else False
                if prev is not None and c != prev:
                    lines.append({"kind": "walk", "covered": prev, "coords": run})
                    run = [run[-1]]
                run.append((lat, lng))
                prev = c
            lines.append({"kind": "walk", "covered": bool(prev), "coords": run})
    return lines


def _waits(it):
    return [s for s in it["segments"] if s["kind"] == "wait" and s.get("lat") is not None]


def _nearest_open(wait, places, status=None):
    status = status or {}
    best = None
    for p in places:
        if status.get(p["key"], {}).get("status") == "uncertain":
            continue
        if open_at(p, wait["start"]) and open_at(p, wait["end"] - timedelta(seconds=1)):
            d = haversine(wait["lat"], wait["lng"], p["lat"], p["lng"])
            if best is None or d < best[0]:
                best = (d, p)
    return best


def _place_view(p, when, status):
    s = status.get(p["key"], {"status": "listed", "reason": "Listing only.", "evidence": []})
    return {"name": p["name"], "category": p.get("category"), "address": p.get("address"), "url": p.get("url"),
            "lat": p["lat"], "lng": p["lng"],
            "hours_today": hours_text(p, when), "status": s["status"], "reason": s["reason"], "evidence": s["evidence"]}


def _option(label, it, places, status, earliest, stride=False):
    pmap = {p["key"]: p for p in places}
    used = {}
    for pt, c in zip(it["points"], it["cover"]):
        for d, k in c[:1]:
            used.setdefault(k, (d, pt["t"]))
    waits = []
    for w in _waits(it):
        n = _nearest_open(w, places, status)
        waits.append({"stop": w["stop"], "start": w["start"].strftime("%-I:%M %p"), "lat": w["lat"], "lng": w["lng"],
                      "covered": bool(n and n[0] <= RADIUS),
                      "minutes": round((w["end"] - w["start"]).total_seconds() / 60, 1),
                      "nearest_open": ({"distance_m": round(n[0]), "walk_min": round(n[0] / 80, 1),
                                        **_place_view(n[1], w["start"], status)} if n else None)})
    # only places this option actually passes (within 150 m of its points) are listed as not counted
    skipped = [_place_view(pmap[k], it["leave"], status) for k, v in status.items()
               if v["status"] == "uncertain" and k in pmap
               and any(haversine(pt["lat"], pt["lng"], pmap[k]["lat"], pmap[k]["lng"]) <= 150 for pt in it["points"])]
    on_foot = round(sum(p["secs"] for p in it["points"]) / 60, 1)
    return {
        "label": label,
        "leave_at": it["leave"].strftime("%-I:%M %p"),
        "arrive_at": it["arrive"].strftime("%-I:%M %p"),
        "total_minutes": round((it["arrive"] - it["leave"]).total_seconds() / 60),
        "extra_minutes": round((it["arrive"] - earliest).total_seconds() / 60),
        "minutes_on_foot_or_waiting": on_foot,
        "minutes_without_open_door": it["stride_uncovered"] if stride else it["uncovered"],
        "wait_inside": it["wait_inside"] if stride else [],
        "source": it.get("source"),
        "route": " → ".join(f"{s['line']}" for s in it["segments"] if s["kind"] == "ride") or "Walk only",
        "modes": sorted({(s.get("vehicle") or "").replace("_", " ").title() for s in it["segments"] if s["kind"] == "ride"}),
        "walk_minutes": round(sum((s["end"] - s["start"]).total_seconds() for s in it["segments"] if s["kind"] == "walk") / 60, 1),
        "wait_minutes": round(sum((s["end"] - s["start"]).total_seconds() for s in it["segments"] if s["kind"] == "wait") / 60, 1),
        "segments": public(it),
        "waits": waits,
        "open_doors": [{**_place_view(pmap[k], t, status), "distance_m": d, "at": t.strftime("%-I:%M %p")}
                       for k, (d, t) in used.items()],
        "skipped_places": skipped,
        "map": _map(it, places, status),
        "conditions": conditions(*_start(it), it["leave"]),
        "air": air_outside(it["points"]),
    }


def _start(it):
    w = next((s for s in it["segments"] if s["kind"] == "walk" and s.get("points")), None)
    return w["points"][0] if w else (37.3874, -122.0398)
