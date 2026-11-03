"""Part 2 — the two generated application outputs, from real data.

Not mockups. Both are produced from the actual 19 July 2024 case the evaluation
found, where community-aware routing changed the recommendation and was right.

  DEN -> SEA, decision at 15:00Z
    baseline picks DEN -> SLC -> SEA because SLC looks cheapest. SLC is in the
    same community as DEN, and the itinerary broke.
    community-aware picks the direct DEN -> SEA, slightly higher per-leg risk,
    different community. It held.

Writes results/app_outputs.json for the figure script.

Run:  PYTHONPATH=src .venv/bin/python notebooks/11_app_outputs.py
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np

from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "app_outputs.json"
ORIGIN, DEST, HOUR = "DEN", "SEA", 15


def main() -> None:
    con = duckdb.connect()
    xy = C.coords()
    d = C.load_day(C.STORM_DAY, con)
    state = C.airport_hour_state(d["flights"])

    comm = {}
    for h in sorted({h for _, h in state}):
        g = C.build_airport_graph(state, h, xy)
        if g.vcount() >= 5:
            comm[h] = dict(zip(g.vs["name"], C.detect(g, "leiden")))

    hrs = sorted({f["sched_dep_hour"] for f in d["flights"]})
    h0 = [h for h in hrs if h.hour == HOUR][0]
    opts = C.build_itineraries(d["flights"], state, comm, ORIGIN, DEST, h0, xy)
    prev = h0 - dt.timedelta(hours=1)
    memb = comm.get(prev, {})

    def hub_community(o):
        """The community actually used for THIS option's own decision hour.

        Options depart in different hours, so each was judged against its own hour's
        membership. Reading them all from one hour made a hub look unclassified while
        still being marked same-community.
        """
        if not o["hub"]:
            return None
        h = o["legs"][0]["sched_dep_hour"] - dt.timedelta(hours=1)
        return comm.get(h, {}).get(o["hub"])

    base = min(opts, key=lambda o: (o["base"], o["n_legs"]))
    best = min(opts, key=lambda o: (o["community"], o["n_legs"]))

    # one row per distinct hub, keeping the lowest-risk option through it
    by_hub: dict = {}
    for o in opts:
        k = o["hub"] or "DIRECT"
        if k not in by_hub or o["base"] < by_hub[k]["base"]:
            by_hub[k] = o
    routes = []
    for k, o in sorted(by_hub.items(), key=lambda kv: kv[1]["base"]):
        routes.append({
            "hub": None if k == "DIRECT" else k,
            "depart": f"{o['legs'][0]['dep_utc']:%H:%M}Z",
            "risk": round(o["base"], 3),
            "same_community": bool(o["same_comm"]),
            "held": bool(o["held"]),
            "is_baseline_pick": o["legs"] == base["legs"],
            "is_community_pick": o["legs"] == best["legs"],
            "hub_lat": xy[k][0] if k != "DIRECT" and k in xy else None,
            "hub_lon": xy[k][1] if k != "DIRECT" and k in xy else None,
            "hub_community": hub_community(o),
            "origin_community_at_option": (
                comm.get(o["legs"][0]["sched_dep_hour"] - dt.timedelta(hours=1), {})
                .get(ORIGIN)),
        })

    # --- empirical arrival-delay distribution, the L1 approach ------------------
    q = con.execute(f"""
        SELECT quantile_cont(arr_delay_min, [0.10,0.25,0.50,0.75,0.90]) AS qs,
               count(*) AS n
        FROM read_parquet('{C.FLIGHTS}', hive_partitioning=true)
        WHERE cancelled = 0 AND arr_delay_min IS NOT NULL
          AND origin = '{ORIGIN}' AND dest = '{DEST}'
          AND month = 7 AND flight_date < DATE '2024-01-01'
          AND sched_dep_hour BETWEEN {HOUR - 1} AND {HOUR + 1}
    """).fetchone()
    quantiles, n_comparable = [float(x) for x in q[0]], int(q[1])

    # what actually happened on the day, for the same slice
    actual = con.execute(f"""
        SELECT arr_delay_min FROM read_parquet('{C.FLIGHTS}', hive_partitioning=true)
        WHERE cancelled = 0 AND arr_delay_min IS NOT NULL
          AND origin = '{ORIGIN}' AND dest = '{DEST}'
          AND flight_date = DATE '{C.STORM_DAY}'
    """).fetchall()

    res = {
        "case": {"origin": ORIGIN, "dest": DEST, "day": C.STORM_DAY,
                 "decision_hour": f"{h0:%H}:00Z",
                 "origin_community": memb.get(ORIGIN),
                 "origin_lat": xy[ORIGIN][0], "origin_lon": xy[ORIGIN][1],
                 "dest_lat": xy[DEST][0], "dest_lon": xy[DEST][1],
                 "n_itineraries": len(opts)},
        "routes": routes,
        "forecast": {"quantiles": {k: round(v, 1) for k, v in
                                   zip(["p10", "p25", "p50", "p75", "p90"], quantiles)},
                     "n_comparable": n_comparable,
                     "actual_today": [int(a[0]) for a in actual]},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2))

    print(f"{ORIGIN} -> {DEST}, {C.STORM_DAY}, decision {h0:%H}:00Z")
    print(f"  {ORIGIN} is in community {memb.get(ORIGIN)}")
    for r in routes:
        mark = ("  <- baseline" if r["is_baseline_pick"] else "") + \
               ("  <- community" if r["is_community_pick"] else "")
        print(f"   via {str(r['hub'] or 'direct'):<7} comm {str(r['hub_community']):>4}  "
              f"risk {r['risk']:.2f}  same={str(r['same_community'])[0]}  "
              f"held={r['held']}{mark}")
    print(f"  L1 comparables n={n_comparable}: {res['forecast']['quantiles']}")
    print(f"  actually flew today: {len(actual)} flights, "
          f"delays {sorted(int(a[0]) for a in actual)}")
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
