from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from .core import ROOT
from .plan import plan
from .routes import TZ, fetch_routes, build_itinerary, public

app = FastAPI(title="Stride")


class PlanRequest(BaseModel):
    origin: str
    destination: str
    end_time: str  # "2026-11-14T19:00" local time
    max_extra_minutes: int = 10
    mode: str = "transit"  # "transit" or "walk"


import json as _json, re as _re

_norm = lambda t: _re.sub(r"[^a-z0-9]", "", (t or "").lower())
DEMOS = {}
for _f in (ROOT / "demo").glob("*.json"):
    _d = _json.loads(_f.read_text())
    _r = _d["request"]
    DEMOS[(_norm(_r["origin"]), _norm(_r["destination"]), _r["end_time"], _r["mode"])] = _d["response"]


@app.post("/api/plan")
def api_plan(req: PlanRequest):
    saved = DEMOS.get((_norm(req.origin), _norm(req.destination), req.end_time[:16], req.mode))
    if saved:
        return saved
    try:
        end = datetime.fromisoformat(req.end_time).replace(tzinfo=TZ)
        return plan(req.origin, req.destination, end, req.max_extra_minutes, mode=req.mode)
    except KeyError as e:
        raise HTTPException(500, f"Missing environment variable {e}")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/app")
def phone_app():
    return FileResponse(ROOT / "static" / "app.html", headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


class DurationRequest(BaseModel):
    origin: str
    destination: str
    end_time: str


@app.post("/api/durations")
def api_durations(req: DurationRequest):
    """Fastest transit and walking time, like the mode tabs in Google Maps (Google Routes only, fast)."""
    end = datetime.fromisoformat(req.end_time).replace(tzinfo=TZ)
    out = {}
    try:
        t = fetch_routes(req.origin, req.destination, end)
        if t:
            it = build_itinerary(t[0], end)
            out["transit"] = round((it["arrive"] - it["leave"]).total_seconds() / 60)
        w = fetch_routes(req.origin, req.destination, end, travel_mode="WALK")
        if w:
            out["walk"] = round(int(str(w[0]["duration"]).rstrip("s")) / 60)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    return out


class GoogleRequest(BaseModel):
    origin: str
    destination: str
    end_time: str
    mode: str = "transit"


@app.post("/api/google")
def api_google(req: GoogleRequest):
    """Google's first route only (Google Routes, under a second) so the app can show it while Stride checks places."""
    end = datetime.fromisoformat(req.end_time).replace(tzinfo=TZ)
    saved = DEMOS.get((_norm(req.origin), _norm(req.destination), req.end_time[:16], req.mode))
    if saved:
        g = next(o for o in saved["options"] if o["role"] == "google")
        return {"option": g}
    try:
        routes = fetch_routes(req.origin, req.destination, end, travel_mode="WALK" if req.mode == "walk" else "TRANSIT")
    except (KeyError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    if not routes:
        raise HTTPException(404, "No route found for this trip and time.")
    it = build_itinerary(routes[0], end)
    lines = [{"kind": s["kind"], "coords": s.get("points") or [], "label": s.get("line"), "covered": None}
             for s in it["segments"] if s["kind"] in ("walk", "ride") and s.get("points")]
    segs = public(it)
    walk = sum(s["minutes"] for s in segs if s["kind"] == "walk")
    return {"option": {"role": "google", "label": "Google's first pick", "leave_at": it["leave"].strftime("%-I:%M %p"),
                       "arrive_at": it["arrive"].strftime("%-I:%M %p"),
                       "total_minutes": round((it["arrive"] - it["leave"]).total_seconds() / 60),
                       "segments": segs, "map": lines, "waits": [], "open_doors": [], "skipped_places": [],
                       "walk_minutes": round(walk, 1), "minutes_without_open_door": None, "partial": True}}
