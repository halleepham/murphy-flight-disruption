"""Murphy — Challenge 4 Human–AI Co-Design app.

A small FastAPI server over `results/codesign/` (written by
`murphy.codesign_export`) and a hand-written front end in `static/`. The server does
the arithmetic — deadline shares, re-ranking after a traveller rules out a hub — so the
browser only displays. No LLM is called at request time; captions were generated and
checked at export time.

Run:  .venv/bin/python -m uvicorn app.web.server:app --port 8000
      then open http://localhost:8000
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / "results" / "codesign"
STATIC = pathlib.Path(__file__).resolve().parent / "static"
FEEDBACK = DATA / "feedback.jsonl"


def _load(name):
    return json.loads((DATA / name).read_text())


TRIPS = {t["id"]: t for t in _load("trips.json")}
REPLAY = _load("replay.json")
CAPTIONS = _load("captions.json")
POLICY = _load("policy.json")
AIRPORTS = _load("airports.json")

app = FastAPI(title="Murphy")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/trips")
def trips():
    """What the trip-input form may offer: only trips with computed results."""
    out = []
    for t in TRIPS.values():
        booked = next(o for o in t["options"] if o["booked"])
        out.append({"id": t["id"], "origin": t["origin"], "dest": t["dest"],
                    "day": t["day"], "day_title": t["day_title"],
                    "decision": t["decision"],
                    "booked": [f"{l['carrier']} {l['flight_number']}" for l in booked["legs"]],
                    "booked_dep": booked["legs"][0]["dep"],
                    "booked_arr": booked["legs"][-1]["arr"],
                    "booked_dep_local": booked["legs"][0]["dep_local"],
                    "decision_local": t["decision_local"]})
    return out


def _deadline(t, minutes_after_sched):
    """Share of comparable 2023 flights that arrived later than the traveller's line."""
    c = t["final_leg_comparables"]
    xs = c["arr_delays"]
    if minutes_after_sched is None or not xs:
        return None
    late = sum(1 for x in xs if x > minutes_after_sched)
    return {"minutes": minutes_after_sched, "n": len(xs), "late": late,
            "share": round(late / len(xs), 3)}


def _quantiles(xs):
    if not xs:
        return None
    s = sorted(xs)
    q = lambda p: s[min(len(s) - 1, int(round(p * (len(s) - 1))))]
    return {"p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90)}


def _rank(options, excluded):
    """Re-rank on the exported scores. Excluded hubs drop out; nothing is re-scored."""
    keep = [o for o in options if not (o["hub"] and o["hub"] in excluded)]
    keep.sort(key=lambda o: (o["score"], len(o["legs"])))
    return keep


@app.get("/api/trip/{trip_id}")
def trip(trip_id: str, deadline: int | None = None, exclude: str = ""):
    t = TRIPS.get(trip_id)
    if not t:
        raise HTTPException(404, "unknown trip")
    excluded = {x for x in exclude.split(",") if x}
    opts = [dict(o) for o in _rank(t["options"], excluded)]
    for o in opts:
        o["recommended_now"] = False
    if opts:
        opts[0]["recommended_now"] = True
    booked = next(o for o in t["options"] if o["booked"])
    carriers = sorted({l["carrier"] for o in [booked] + opts[:1] for l in o["legs"]})
    c = t["final_leg_comparables"]
    return {**{k: v for k, v in t.items() if k not in ("options", "final_leg_comparables")},
            "options": opts, "excluded": sorted(excluded),
            "booked": booked,
            "band": {"route": c["route"], "month": c["month"], "hour_local": c["hour_local"],
                     "n": c["n"], "quantiles": _quantiles(c["arr_delays"]),
                     "histogram": _hist(c["arr_delays"])},
            "deadline": _deadline(t, deadline),
            "carriers": carriers}


def _hist(xs, lo=-40, hi=180, step=10):
    bins = list(range(lo, hi + step, step))
    counts = [0] * (len(bins) - 1)
    for x in xs:
        x = min(max(x, lo), hi - 1)
        counts[(x - lo) // step] += 1
    return {"edges": bins, "counts": counts}


@app.get("/api/policy/{carrier}")
def policy(carrier: str):
    ids = POLICY["by_carrier"].get(carrier.upper(), [])
    clauses = [POLICY["clauses"][i] for i in ids if i in POLICY["clauses"]]
    clauses.sort(key=lambda c: (c["source"] != "DOT Airline Customer Service Dashboard",
                                c["disruption_type"], c["name"]))
    return {"carrier": carrier.upper(), "clauses": clauses, "note": POLICY["note"]}


@app.get("/api/replay/{label}")
def replay(label: str):
    r = REPLAY.get(label)
    if not r:
        raise HTTPException(404, "unknown day")
    return {**r, "captions": CAPTIONS.get(label, [])}


@app.get("/api/airports")
def airports():
    return AIRPORTS


class Feedback(BaseModel):
    trip_id: str | None = None
    day: str | None = None
    kind: str                     # exclude_hub | reject_pick | accept_pick | flag_caption
    target: str | None = None
    reason: str | None = None


@app.post("/api/feedback")
def feedback(f: Feedback):
    rec = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           **f.model_dump()}
    with FEEDBACK.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    n = sum(1 for _ in FEEDBACK.open())
    return {"ok": True, "logged": rec, "total_feedback": n}


@app.get("/api/feedback")
def feedback_log():
    if not FEEDBACK.exists():
        return []
    return [json.loads(l) for l in FEEDBACK.open() if l.strip()]
