"""Challenge 4 Stage 3 — export everything the Co-Design web app shows.

The app (`app/web/`) never computes a risk number itself. Everything it displays comes
from this file, written to `results/codesign/`:

  trips.json      sample trips on the three test days, each with ranked options,
                  per-leg reasons (F4), the abnormality banner (F6) as of the
                  decision hour, and comparable-flight arrival delays (F2)
  replay.json     hour-by-hour airport states and region summaries for the day
                  replay (G1), plus the computed key events
  captions.json   AI-written captions for those events (G1a), each checked so it
                  contains no number absent from the events it was given
  policy.json     PolicyClause records per carrier (F3), from kg_policy. Stands in
                  for Neo4j, whose Aura instance was cleared on 3 Nov

Regions are the FIXED map (Leiden on distance alone), not the hourly communities:
A21 showed the fixed map wins on all three days.

Run:  PYTHONPATH=src .venv/bin/python -m murphy.codesign_export [--no-captions]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import re
import warnings
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import duckdb
import igraph as ig
import numpy as np

from murphy import anomaly as A
from murphy import communities as C

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "codesign"
DAYS = (("weather", C.WEATHER_DAY, "Winter storm"),
        ("storm", C.STORM_DAY, "CrowdStrike outage"),
        ("calm", C.CALM_DAY, "Ordinary Tuesday"))
ALARM_SHARE = 0.25          # same threshold anomaly.py uses for the lead-time alarm
MIN_SCORED = 30             # an hour with fewer scored airports is too thin to headline


def fixed_regions(airports, xy, seed=42):
    """Leiden on distance alone. Same as notebooks/14_fixed_vs_dynamic.py."""
    aps = sorted(a for a in airports if a in xy)
    g = ig.Graph(); g.add_vertices(len(aps)); g.vs["name"] = aps
    edges, w = [], []
    for i in range(len(aps)):
        for j in range(i + 1, len(aps)):
            d = C.great_circle_km(aps[i], aps[j], xy)
            if d > C.GEO_CUT * C.GEO_SIGMA_KM:
                continue
            val = float(np.exp(-((d / C.GEO_SIGMA_KM) ** 2)))
            if val > 0.01:
                edges.append((i, j)); w.append(val)
    g.add_edges(edges); g.es["weight"] = w
    return dict(zip(aps, C.detect(g, "leiden", seed=seed)))


# DuckDB hands back timestamptz in the SESSION zone (America/Chicago here), so a bare
# strftime prints Central time. Convert explicitly before labelling anything UTC (A25).
UTC = dt.timezone.utc


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def hhmm(t):
    return t.astimezone(UTC).strftime("%H:%MZ")


def local(t, tz):
    """Clock time at the airport, e.g. '07:35 MDT' -- what a traveller reads."""
    return t.astimezone(ZoneInfo(tz)).strftime("%H:%M %Z") if tz else None


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def build_replay(label, day, title, state, zrows, regions, xy):
    hours = sorted({h for _, h in state})
    z_by = {(r["airport"], r["hour"]): r for r in zrows}
    frames = []
    for h in hours:
        aps = {}
        for (ap, hh), v in state.items():
            if hh != h or ap not in xy:
                continue
            z = z_by.get((ap, h))
            aps[ap] = {"late": round(v["late_share"], 3), "n": v["n"],
                       "z": round(z["z"], 1) if z else None,
                       "flag": bool(z["flag"]) if z else False}
        scored = [a for a in aps.values() if a["z"] is not None]
        flagged = sum(a["flag"] for a in scored)
        reg = collections.defaultdict(lambda: [0, 0, []])
        for ap, a in aps.items():
            if ap in regions:
                r = reg[regions[ap]]
                r[0] += 1; r[1] += int(a["flag"]); r[2].append(a["late"])
        frames.append({
            "i": len(frames),
            "hour": hhmm(h),
            "airports": aps,
            "scored": len(scored), "flagged": flagged,
            "share_flagged": round(flagged / len(scored), 3) if scored else 0.0,
            "mean_late": round(float(np.mean([a["late"] for a in aps.values()])), 3)
                         if aps else 0.0,
            "regions": {str(k): {"airports": v[0], "flagged": v[1],
                                 "mean_late": round(float(np.mean(v[2])), 3)}
                        for k, v in reg.items()},
        })

    # key events -- computed, then handed to the LLM as the only facts it may use
    events = []
    alarm = next((f for f in frames if f["scored"] >= MIN_SCORED
                  and f["share_flagged"] >= ALARM_SHARE), None)
    solid = [f for f in frames if f["scored"] >= MIN_SCORED]
    peak = max(solid, key=lambda f: f["mean_late"])
    if alarm:
        top = sorted(((ap, a) for ap, a in alarm["airports"].items() if a["flag"]),
                     key=lambda x: -x[1]["z"])[:4]
        events.append({"hour": alarm["hour"], "kind": "alarm",
                       "share_flagged_pct": round(100 * alarm["share_flagged"]),
                       "airports_flagged": alarm["flagged"],
                       "airports_scored": alarm["scored"],
                       "most_unusual": [ap for ap, _ in top]})
    # the worst region at the busiest abnormal hour
    worst = max(solid, key=lambda f: f["flagged"])
    if worst["flagged"]:
        rk, rv = max(worst["regions"].items(), key=lambda kv: kv[1]["flagged"])
        members = sorted([ap for ap, r in regions.items() if str(r) == rk
                          and ap in worst["airports"]],
                         key=lambda ap: -worst["airports"][ap]["late"])[:4]
        events.append({"hour": worst["hour"], "kind": "worst_hour",
                       "airports_flagged": worst["flagged"],
                       "airports_scored": worst["scored"],
                       "region": int(rk), "region_flagged": rv["flagged"],
                       "region_airports": rv["airports"],
                       "region_examples": members})
    events.append({"hour": peak["hour"], "kind": "peak_lateness",
                   "mean_late_pct": round(100 * peak["mean_late"]),
                   # from the instants, not the labels: the day crosses midnight UTC
                   "lead_hours_after_alarm": (
                       int((hours[peak["i"]] - hours[alarm["i"]]).total_seconds() // 3600)
                       if alarm else None)})
    if not alarm:
        events.insert(0, {"hour": solid[0]["hour"], "kind": "no_alarm",
                          "max_share_flagged_pct":
                              round(100 * max(f["share_flagged"] for f in solid))})
    idx = {f["hour"]: f["i"] for f in frames}
    for e in events:
        e["i"] = idx[e["hour"]]
    events.sort(key=lambda e: e["i"])
    return {"label": label, "day": day, "title": title,
            "alarm_hour": alarm["hour"] if alarm else None,
            "regions": {ap: r for ap, r in regions.items()},
            "region_count": len(set(regions.values())),
            "frames": frames, "events": events}


# ---------------------------------------------------------------------------
# trips
# ---------------------------------------------------------------------------
_LH = {}


def local_hour(con, f):
    """The Parquet `sched_dep_hour` is LOCAL at the origin -- match it in kind."""
    if f["flight_id"] not in _LH:
        _LH[f["flight_id"]] = con.execute(f"""
            SELECT sched_dep_hour FROM read_parquet('{C.FLIGHTS}', hive_partitioning=true)
            WHERE flight_date = DATE '{f['flight_id'][:10]}'
              AND flight_id = '{f['flight_id']}'""").fetchone()[0]
    return _LH[f["flight_id"]]


def comparables(con, origin, dest, month, hour):
    """2023 arrival delays on this route, same month, departure hour +-1 (L1)."""
    rows = con.execute(f"""
        SELECT arr_delay_min
        FROM read_parquet('{C.FLIGHTS}', hive_partitioning=true)
        WHERE cancelled = 0 AND arr_delay_min IS NOT NULL
          AND origin = '{origin}' AND dest = '{dest}' AND month = {month}
          AND flight_date < DATE '2024-01-01'
          AND sched_dep_hour BETWEEN {hour - 1} AND {hour + 1}
    """).fetchall()
    return sorted(int(round(r[0])) for r in rows)


def leg_record(f, extra, state, z_by, regions, prev_leg, decision):
    h = f["sched_dep_hour"] - dt.timedelta(hours=1)
    st = state.get((f["origin"], h))
    z = z_by.get((f["origin"], h))
    inbound = None
    if prev_leg:
        p = prev_leg
        actual_dep = (p["dep_utc"] + dt.timedelta(minutes=float(p["dep_delay"]))
                      if p["dep_delay"] is not None else None)
        if p["cancelled"]:
            status = "cancelled" if p["dep_utc"] <= decision else "not yet due"
        elif actual_dep is not None and actual_dep <= decision:
            status = f"left {p['origin']} {int(p['dep_delay'])} min late" \
                if p["dep_delay"] >= C.LATE_MIN else f"left {p['origin']} on time"
        else:
            status = "not yet departed"
        inbound = {"from": p["origin"], "sched_dep": hhmm(p["dep_utc"]),
                   "sched_dep_local": local(p["dep_utc"], extra.get(p["flight_id"], {}).get("otz")),
                   "status_as_of_decision": status}
    return {"carrier": f["carrier"], "flight_number": extra.get(f["flight_id"], {}).get("fn"),
            "origin": f["origin"], "dest": f["dest"],
            "dep": iso(f["dep_utc"]), "arr": iso(f["arr_utc"]),
            "dep_local": local(f["dep_utc"], extra.get(f["flight_id"], {}).get("otz")),
            "arr_local": local(f["arr_utc"], extra.get(f["flight_id"], {}).get("dtz")),
            "origin_late_hour_before": round(st["late_share"], 3) if st else None,
            "origin_flights_hour_before": st["n"] if st else None,
            "origin_z_hour_before": round(z["z"], 1) if z else None,
            "origin_region": regions.get(f["origin"]),
            "inbound_aircraft": inbound,
            # hindsight, shown only in the reveal
            "outcome_dep_delay": f["dep_delay"], "outcome_arr_delay": f["arr_delay"],
            "outcome_cancelled": bool(f["cancelled"])}


def build_trips(label, day, title, d, state, zrows, regions, xy, con, want):
    flights = d["flights"]
    extra = {r[0]: {"fn": int(r[1]) if r[1] is not None else None,
                    "otz": r[2], "dtz": r[3]}
             for r in con.execute(f"""
                SELECT flight_id, flight_number, origin_tz, dest_tz
                FROM read_parquet('{C.FLIGHTS}', hive_partitioning=true)
                WHERE flight_date = DATE '{day}'""").fetchall()}
    by_tail = collections.defaultdict(list)
    for f in flights:
        if f["tail"]:
            by_tail[f["tail"]].append(f)
    for v in by_tail.values():
        v.sort(key=lambda f: f["dep_utc"])
    prev_of = {}
    for v in by_tail.values():
        for a, b in zip(v, v[1:]):
            prev_of[b["flight_id"]] = a

    z_by = {(r["airport"], r["hour"]): r for r in zrows}
    hours = sorted({h for _, h in state})
    comm = {h: regions for h in hours}
    majors = [f for f in flights if f["carrier"] in C.MAJORS]
    pairs = collections.Counter((f["origin"], f["dest"]) for f in majors)
    hrs = sorted({f["sched_dep_hour"] for f in majors})
    cases = [(o, dd, h) for (o, dd), _ in pairs.most_common(40) for h in hrs[::3]]

    found = []
    for o, dd, h in cases:
        opts = C.build_itineraries(flights, state, comm, o, dd, h, xy)
        if len(opts) < 3 or not any(x["hub"] for x in opts):
            continue
        b = min(opts, key=lambda x: (x["base"], x["n_legs"]))
        c = min(opts, key=lambda x: (x["community"], x["n_legs"]))
        changed = b["legs"] != c["legs"]
        kind = ("better" if changed and c["held"] and not b["held"]
                else "same" if not changed and b["hub"] and b["held"] else None)
        if kind != want[0]:
            continue
        # a trip must have a banner and enough comparables to draw a band
        prev_rows = sum(1 for r in zrows if r["hour"] == h - dt.timedelta(hours=1))
        last = b["legs"][-1]
        if prev_rows < MIN_SCORED or b["legs"][0]["dep_utc"].date() != h.date():
            continue
        if len(comparables(con, last["origin"], last["dest"], last["dep_utc"].month,
                           local_hour(con, last))) < 20:
            continue
        if True:
            found.append((o, dd, h, opts, b, c, kind))
    # prefer varied routes, and cases with a real choice
    seen, chosen = set(), []
    for item in sorted(found, key=lambda x: -len(x[3])):
        if item[0] in seen or item[1] in seen:
            continue
        seen |= {item[0], item[1]}
        chosen.append(item)
        if len(chosen) == want[1]:
            break

    trips = []
    for o, dd, h, opts, b, c, kind in chosen:
        decision = h
        # dedupe to one option per (hub, first departure), keep the 7 best + b + c
        ranked = sorted(opts, key=lambda x: (x["community"], x["n_legs"]))
        keep, keys = [], set()
        for x in [b, c] + ranked:
            k = (x["hub"], x["legs"][0]["flight_id"], x["legs"][-1]["flight_id"])
            if k in keys:
                continue
            keys.add(k); keep.append(x)
            if len(keep) >= 8:
                break
        keep.sort(key=lambda x: (x["community"], x["n_legs"]))
        options = []
        for x in keep:
            legs = [leg_record(l, extra, state, z_by, regions, prev_of.get(l["flight_id"]),
                               decision) for l in x["legs"]]
            options.append({
                "id": "-".join(str(l["flight_id"]) for l in x["legs"]),
                "hub": x["hub"], "legs": legs,
                "hub_lat": xy[x["hub"]][0] if x["hub"] else None,
                "hub_lon": xy[x["hub"]][1] if x["hub"] else None,
                "hub_region": regions.get(x["hub"]) if x["hub"] else None,
                "base_risk": round(x["base"], 3),
                "same_region": bool(x["same_comm"]),
                "score": round(x["community"], 3),
                "outcome_held": bool(x["held"]),
                "booked": x is b, "recommended": x is c})
        last = b["legs"][-1]
        first = b["legs"][0]
        lh = local_hour(con, last)
        cmp_ = comparables(con, last["origin"], last["dest"], first["dep_utc"].month, lh)
        # banner as of the decision: the hour before it is the last complete one
        prev_h = decision - dt.timedelta(hours=1)
        hz = [r for r in zrows if r["hour"] == prev_h]
        alarm_hours = sorted({r["hour"] for r in zrows})
        first_alarm = None
        for ah in alarm_hours:
            if ah > prev_h:
                break
            rows = [r for r in zrows if r["hour"] == ah]
            if len(rows) >= MIN_SCORED and np.mean([r["flag"] for r in rows]) >= ALARM_SHARE:
                first_alarm = hhmm(ah); break
        trips.append({
            "id": f"{label}-{o}-{dd}-{decision.astimezone(UTC):%H}",
            "decision_local": local(decision, extra.get(b["legs"][0]["flight_id"], {}).get("otz")),
            "day_label": label, "day": day, "day_title": title,
            "origin": o, "dest": dd, "decision": iso(decision),
            "origin_lat": xy[o][0], "origin_lon": xy[o][1],
            "dest_lat": xy[dd][0], "dest_lon": xy[dd][1],
            "origin_region": regions.get(o), "case_kind": kind,
            "n_options_total": len(opts),
            "banner": {"as_of": hhmm(prev_h), "scored": len(hz),
                       "flagged": int(sum(r["flag"] for r in hz)),
                       "share_flagged": round(float(np.mean([r["flag"] for r in hz])), 3)
                                        if hz else None,
                       "first_alarm": first_alarm},
            "final_leg_comparables": {"route": f"{last['origin']}-{last['dest']}",
                                      "month": first["dep_utc"].month,
                                      "hour_local": lh,
                                      "n": len(cmp_), "arr_delays": cmp_},
            "options": options})
    return trips


# ---------------------------------------------------------------------------
# captions (G1a)
# ---------------------------------------------------------------------------
NUM = re.compile(r"\d+(?:\.\d+)?")


def _allowed_numbers(events, title, day):
    s = json.dumps(events) + " " + title + " " + day
    return set(NUM.findall(s)) | {"2024", str(C.LATE_MIN)}   # the 15-min rule is stated in the prompt


def template_caption(e):
    k = e["kind"]
    if k == "alarm":
        return (f"Alarm at {e['hour']}: {e['share_flagged_pct']}% of scored airports are "
                f"running late in a way that is unusual for them at this hour. "
                f"Most unusual: {', '.join(e['most_unusual'])}.")
    if k == "worst_hour":
        return (f"{e['hour']}: {e['airports_flagged']} of {e['airports_scored']} airports "
                f"abnormal. Region {e['region']} is hit hardest — {e['region_flagged']} of "
                f"its {e['region_airports']} airports, including "
                f"{', '.join(e['region_examples'])}.")
    if k == "peak_lateness":
        tail = (f", {e['lead_hours_after_alarm']} hours after the alarm"
                if e.get("lead_hours_after_alarm") is not None else "")
        return f"Lateness peaks at {e['hour']}: {e['mean_late_pct']}% of departures late{tail}."
    if k == "no_alarm":
        return (f"No alarm all day. At most {e['max_share_flagged_pct']}% of airports are "
                f"ever abnormal — a normal day, so the forecast's history applies.")
    return ""


def llm_captions(replays):
    from groq import Groq
    from murphy.config import load_env
    key = load_env("GROQ_API_KEY")
    if not key:
        return None
    client = Groq(api_key=key)
    out = {}
    for r in replays:
        facts = {"day": r["day"], "title": r["title"], "events": r["events"]}
        prompt = (
            "You write one-sentence captions for an animated map of US flight delays, "
            "for travellers, plain English, at most 30 words each. Use ONLY the facts "
            "in this JSON. Do not add any number, airport, airline or cause that is "
            "not in it — if a cause is not stated, do not guess one. 'Abnormal' means "
            "an AIRPORT running late in a way unusual for it at that hour. Units: "
            "share_flagged_pct and airports_flagged count AIRPORTS, not flights; "
            "mean_late_pct is the percent of DEPARTURES that left 15+ minutes late. "
            "Hours are UTC; write them like '05:00 UTC'. Regions are unnamed groups of "
            "nearby airports; call them 'one group of nearby airports', never 'region 2'. "
            "'worst_hour' is the hour with the MOST airports flagged — still say the count, never 'most airports'. "
            "Percentages of airports are of the airports SCORED that hour; never say "
            "'the network', 'the country' or 'all flights'. "
            "Return JSON: "
            '{"captions": ["...", ...]} with one caption per event, same order.\n\n'
            + json.dumps(facts))
        try:
            resp = client.chat.completions.create(
                model="openai/gpt-oss-120b", temperature=0.2,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}])
            caps = json.loads(resp.choices[0].message.content)["captions"]
        except Exception as exc:                       # noqa: BLE001
            print(f"  caption call failed for {r['label']}: {exc}")
            caps = []
        allowed = _allowed_numbers(r["events"], r["title"], r["day"])
        rows = []
        for i, e in enumerate(r["events"]):
            cap = caps[i] if i < len(caps) else None
            bad = sorted(set(NUM.findall(cap or "")) - allowed) if cap else ["missing"]
            # airports must be ones the events named
            named = set(re.findall(r"\b[A-Z]{3}\b", json.dumps(e)))
            bad_ap = sorted(set(re.findall(r"\b[A-Z]{3}\b", cap or "")) - named
                            - {"USA", "UTC"}) if cap else []
            ok = not bad and not bad_ap
            rows.append({"hour": e["hour"], "kind": e["kind"],
                         "text": cap if ok else template_caption(e),
                         "source": "llm" if ok else "template",
                         "rejected": None if ok else {"text": cap,
                                                      "unseen_numbers": bad,
                                                      "unseen_airports": bad_ap},
                         "facts": e})
        out[r["label"]] = rows
    return out


# ---------------------------------------------------------------------------
def export_policy():
    from murphy import kg_policy as K
    clauses, links = K.parse_dashboard()
    c2, l2 = K.parse_contracts()
    by_id = {c["clause_id"]: c for c in clauses + c2}
    per = collections.defaultdict(list)
    for l in links + l2:
        per[l["carrier"]].append(l["clause_id"])
    return {"clauses": by_id, "by_carrier": per,
            "note": "Documented tier: quoted with citation, never combined with a number. "
                    "Exported from local sources; live Neo4j later."}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-captions", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    xy = C.coords()
    print("fitting 2023 anomaly baseline ...")
    base = A.fit_baseline(con)
    want = {"weather": ("better", 2), "storm": ("better", 2), "calm": ("same", 1)}

    trips, replays = [], []
    for label, day, title in DAYS:
        d = C.load_day(day, con)
        state = C.airport_hour_state(d["flights"])
        regions = fixed_regions(sorted({a for a, _ in state}), xy)
        zrows = A.score_day(day, base, con)
        replays.append(build_replay(label, day, title, state, zrows, regions, xy))
        t = build_trips(label, day, title, d, state, zrows, regions, xy, con, want[label])
        trips += t
        print(f"  {label}: {len(t)} trips, {replays[-1]['region_count']} regions, "
              f"alarm {replays[-1]['alarm_hour']}")

    (OUT / "trips.json").write_text(json.dumps(trips, indent=1))
    (OUT / "replay.json").write_text(json.dumps(
        {r["label"]: r for r in replays}, separators=(",", ":")))
    (OUT / "policy.json").write_text(json.dumps(export_policy(), indent=1))
    (OUT / "airports.json").write_text(json.dumps(
        {k: v for k, v in xy.items()}, separators=(",", ":")))

    caps = None if args.no_captions else llm_captions(replays)
    if caps is None:
        caps = {r["label"]: [{"hour": e["hour"], "kind": e["kind"],
                              "text": template_caption(e), "source": "template",
                              "rejected": None, "facts": e} for e in r["events"]]
                for r in replays}
    (OUT / "captions.json").write_text(json.dumps(caps, indent=1))
    n_llm = sum(c["source"] == "llm" for v in caps.values() for c in v)
    n_all = sum(len(v) for v in caps.values())
    print(f"captions: {n_llm}/{n_all} LLM-written and passed the number check")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
