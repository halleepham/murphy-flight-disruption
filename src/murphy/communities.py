"""Community detection over Murphy's flight network -- Challenge 4, AI Design.

Stage 1 (Human Design) defined two community types and claimed they would make
correlated failure visible. This module builds them and measures whether that claim
holds.

  C1  weather-coupled airports    nodes are airports; edges are :NEAR, a distance
                                  kernel, re-weighted each hour by how similarly the
                                  two airports are actually behaving. That is what
                                  makes a community time-specific rather than a fixed
                                  map: two neighbouring airports both running late are
                                  strongly coupled; the same two on a calm day are not.

  C2  rotation-coupled flights    nodes are flights AND airport-hours; edges are
                                  :PRECEDES (same tail, consecutive legs) and
                                  :DEPARTS_IN / :ARRIVES_IN (a flight touching an
                                  airport-hour). The airport-hour node is the bridge,
                                  so two different aircraft join one community only by
                                  passing through the same congested hour.

**Why flights connect through a node instead of to each other.** Linking every pair of
flights that share an airport-hour is quadratic -- one hour with 50 flights is 1,225
edges. Routing them through the airport-hour node keeps the graph linear and matches
how the Challenge 3 graph is already built.

Scope note: communities are detected from **all** traffic, regionals included, because
network state is a property of the whole airport. Itinerary evaluation stays on the
nine majors, which is Murphy's scope. Same split as the STGNN work (T2).

Run:  PYTHONPATH=src .venv/bin/python -m murphy.communities
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import random
import warnings

warnings.filterwarnings("ignore")

import duckdb
import igraph as ig
import leidenalg
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
FLIGHTS = str(ROOT / "data" / "processed" / "flights_full" / "**" / "*.parquet")
COORDS = ROOT / "data" / "raw" / "airport_coords.csv"

LATE_MIN = 15
GEO_SIGMA_KM = 750.0      # tuned in the STGNN work; wins in combination at every horizon
GEO_CUT = 3.0             # drop edges beyond 3 sigma
MIN_FLIGHTS_HOUR = 3      # an airport-hour below this is too thin to characterise
MAJORS = ("AA", "DL", "UA", "WN", "AS", "B6", "F9", "G4", "HA")

STORM_DAY = "2024-07-19"    # CrowdStrike -- a systems outage, not weather
WEATHER_DAY = "2024-01-15"  # the worst weather day of 2024: 158k minutes of
                            # BTS weather-caused delay, 39.2% late, 15.9% cancelled
CALM_DAY = "2024-10-29"

# Why three days, not two. C1 groups airports that are near each other AND behaving
# alike. Weather is the usual reason nearby airports fail together, but the detector
# never sees a cause -- so validating it only on CrowdStrike tested spatial
# correlation from a SYSTEMS outage, never from weather itself. 15 January is the
# test of the mechanism the design actually claims.


def coords() -> dict[str, tuple[float, float]]:
    with COORDS.open() as f:
        return {r["airport"]: (float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)}


def great_circle_km(a, b, xy) -> float:
    (la1, lo1), (la2, lo2) = xy[a], xy[b]
    return float(np.hypot((la1 - la2) * 111.0,
                          (lo1 - lo2) * 111.0 * np.cos(np.radians((la1 + la2) / 2))))


def load_day(day: str, con=None) -> dict:
    """One day of flights, keyed to ACTUAL event hours where an outcome is involved.

    The as-of rule from the STGNN work applies: a feature read at hour h must be
    complete at the end of hour h. `dep_utc` is the SCHEDULED instant, so observed
    lateness is bucketed by actual departure time, never scheduled.
    """
    con = con or duckdb.connect()
    rows = con.execute(f"""
        SELECT flight_id, carrier, tail_number, origin, dest,
               dep_utc, arr_utc, dep_delay_min, arr_delay_min, cancelled,
               date_trunc('hour', dep_utc) AS sched_dep_hour,
               date_trunc('hour', dep_utc + INTERVAL (coalesce(dep_delay_min, 0)) MINUTE)
                   AS actual_dep_hour,
               date_trunc('hour', arr_utc + INTERVAL (coalesce(arr_delay_min, 0)) MINUTE)
                   AS actual_arr_hour
        FROM read_parquet('{FLIGHTS}', hive_partitioning=true)
        WHERE flight_date = DATE '{day}'
    """).fetchall()
    cols = ["flight_id", "carrier", "tail", "origin", "dest", "dep_utc", "arr_utc",
            "dep_delay", "arr_delay", "cancelled", "sched_dep_hour",
            "actual_dep_hour", "actual_arr_hour"]
    flights = [dict(zip(cols, r)) for r in rows]
    for f in flights:
        f["late"] = (f["dep_delay"] is not None and f["dep_delay"] >= LATE_MIN)
        f["failed"] = bool(f["cancelled"]) or f["late"]
    return {"day": day, "flights": flights}


def airport_hour_state(flights: list[dict]) -> dict:
    """Observed late share per (airport, actual departure hour), plus volume."""
    agg: dict = {}
    for f in flights:
        if f["cancelled"] or f["dep_delay"] is None:
            continue
        key = (f["origin"], f["actual_dep_hour"])
        a = agg.setdefault(key, [0, 0])
        a[0] += 1
        a[1] += int(f["late"])
    return {k: {"n": n, "late_share": lt / n}
            for k, (n, lt) in agg.items() if n >= MIN_FLIGHTS_HOUR}


def build_airport_graph(state: dict, hour, xy) -> ig.Graph:
    """C1 for one hour: near airports, weighted by how alike they are behaving.

    w_ij = geo_kernel(distance) * (1 - |late_i - late_j|)

    The kernel alone would give the same fixed map every hour. The behaviour term is
    what makes the community time-specific: neighbouring airports both running late
    couple strongly; the same pair on a calm day barely couple at all.
    """
    here = {ap: v for (ap, h), v in state.items() if h == hour and ap in xy}
    aps = sorted(here)
    g = ig.Graph()
    g.add_vertices(len(aps))
    g.vs["name"] = aps
    g.vs["late_share"] = [here[a]["late_share"] for a in aps]
    g.vs["n"] = [here[a]["n"] for a in aps]

    edges, weights = [], []
    for i in range(len(aps)):
        for j in range(i + 1, len(aps)):
            d = great_circle_km(aps[i], aps[j], xy)
            if d > GEO_CUT * GEO_SIGMA_KM:
                continue
            geo = float(np.exp(-((d / GEO_SIGMA_KM) ** 2)))
            alike = 1.0 - abs(here[aps[i]]["late_share"] - here[aps[j]]["late_share"])
            w = geo * alike
            if w > 0.01:
                edges.append((i, j))
                weights.append(w)
    g.add_edges(edges)
    g.es["weight"] = weights
    return g


def build_flight_graph(flights: list[dict]) -> ig.Graph:
    """C2: flights and airport-hours as nodes; PRECEDES and DEPARTS/ARRIVES_IN as edges."""
    operated = [f for f in flights if not f["cancelled"] and f["dep_delay"] is not None]
    fid = {f["flight_id"]: i for i, f in enumerate(operated)}

    hours: dict = {}
    def hour_node(ap, h):
        key = (ap, h)
        if key not in hours:
            hours[key] = len(fid) + len(hours)
        return hours[key]

    edges, weights, kinds = [], [], []
    # PRECEDES: consecutive legs of one tail
    by_tail: dict = {}
    for f in operated:
        if f["tail"]:
            by_tail.setdefault(f["tail"], []).append(f)
    for legs in by_tail.values():
        legs.sort(key=lambda f: f["dep_utc"])
        for a, b in zip(legs, legs[1:]):
            edges.append((fid[a["flight_id"]], fid[b["flight_id"]]))
            weights.append(1.0)
            kinds.append("PRECEDES")
    # DEPARTS_IN / ARRIVES_IN: the airport-hour bridge
    for f in operated:
        i = fid[f["flight_id"]]
        edges.append((i, hour_node(f["origin"], f["actual_dep_hour"])))
        weights.append(0.5); kinds.append("DEPARTS_IN")
        if f["actual_arr_hour"] is not None:
            edges.append((i, hour_node(f["dest"], f["actual_arr_hour"])))
            weights.append(0.5); kinds.append("ARRIVES_IN")

    g = ig.Graph()
    g.add_vertices(len(fid) + len(hours))
    names = [None] * (len(fid) + len(hours))
    kind = [None] * len(names)
    for f in operated:
        names[fid[f["flight_id"]]] = f["flight_id"]; kind[fid[f["flight_id"]]] = "flight"
    for (ap, h), idx in hours.items():
        names[idx] = f"{ap}@{h:%H}"; kind[idx] = "airport_hour"
    g.vs["name"] = names
    g.vs["kind"] = kind
    g.add_edges(edges)
    g.es["weight"] = weights
    g.es["kind"] = kinds
    return g


def detect(g: ig.Graph, method: str = "leiden", seed: int = 42, resolution: float = 1.0):
    """Community membership. Leiden by design (Stage 1); Louvain for comparison."""
    if g.vcount() == 0:
        return []
    if method == "leiden":
        part = leidenalg.find_partition(
            g, leidenalg.RBConfigurationVertexPartition,
            weights=g.es["weight"] if "weight" in g.es.attributes() else None,
            resolution_parameter=resolution, seed=seed)
        return list(part.membership)
    if method == "louvain":
        # igraph's Louvain takes no seed argument -- it draws from the stdlib RNG.
        # Without seeding it here the "five seeds" were five unseeded runs, and any
        # finding from them was not reproducible. N11 is the precedent.
        random.seed(seed)
        part = g.community_multilevel(
            weights=g.es["weight"] if "weight" in g.es.attributes() else None,
            resolution=resolution)
        return list(part.membership)
    raise ValueError(method)


def modularity(g: ig.Graph, membership) -> float:
    if g.ecount() == 0 or not membership:
        return float("nan")
    return float(g.modularity(membership, weights=g.es["weight"]))


def connected_communities(g: ig.Graph, membership) -> tuple[int, int]:
    """How many communities are internally connected -- Leiden's guarantee, checked."""
    ok = tot = 0
    for c in set(membership):
        idx = [i for i, m in enumerate(membership) if m == c]
        if len(idx) < 2:
            ok += 1; tot += 1; continue
        tot += 1
        ok += int(g.subgraph(idx).is_connected())
    return ok, tot


if __name__ == "__main__":
    xy = coords()
    con = duckdb.connect()
    for day in (STORM_DAY, CALM_DAY):
        d = load_day(day, con)
        state = airport_hour_state(d["flights"])
        hours = sorted({h for _, h in state})
        mid = [h for h in hours if h.hour == 20]          # 20:00 UTC = afternoon peak
        h = mid[0] if mid else hours[len(hours) // 2]
        g1 = build_airport_graph(state, h, xy)
        g2 = build_flight_graph(d["flights"])
        print(f"\n=== {day} ===")
        print(f"  C1 airports @ {h:%H}:00Z  {g1.vcount()} nodes  {g1.ecount()} edges")
        for m in ("leiden", "louvain"):
            mem = detect(g1, m)
            ok, tot = connected_communities(g1, mem)
            print(f"     {m:<8} {len(set(mem))} communities  "
                  f"modularity {modularity(g1, mem):.3f}  connected {ok}/{tot}")
        print(f"  C2 flights+hours        {g2.vcount()} nodes  {g2.ecount()} edges")
        for m in ("leiden", "louvain"):
            mem = detect(g2, m)
            ok, tot = connected_communities(g2, mem)
            print(f"     {m:<8} {len(set(mem))} communities  "
                  f"modularity {modularity(g2, mem):.3f}  connected {ok}/{tot}")


# ---------------------------------------------------------------------------
# The Stage 1 claim, made measurable
# ---------------------------------------------------------------------------
#
# Stage 1 said: "legs are scored independently, so correlated failure is invisible --
# communities make switching decisions correct." The test is a routing choice.
#
#   A traveller at ORIGIN needs DEST. Candidate itineraries are direct flights and
#   one-stop connections through a hub. Each is ranked, the top pick is taken, and we
#   check against what actually happened that day.
#
#     BASELINE          rank by per-leg risk alone -- the observed late share at each
#                       leg's origin in the hour BEFORE it departs. Legs are scored
#                       independently, which is exactly today's behaviour.
#     COMMUNITY-AWARE   same, plus a penalty when the connecting hub sits in the SAME
#                       C1 community as the origin, i.e. the hub shares the origin's
#                       problem and connecting through it is not an escape.
#
# Note the sign. The reference package boosts same-community results, because its task
# is "find more like this". Murphy's task is "find something that will not break with
# me", so the same mechanism is applied with the opposite sign.

MCT_MIN = 45          # Murphy's single global connection rule
MAX_CONNECT_MIN = 360
PENALTY = 0.25
MAX_DETOUR = 1.5      # a one-stop may not exceed 1.5x the direct great-circle distance

# Without the detour cap the generator offered ATL -> SEA -> EWR as a low-risk way to
# reach Newark. It is technically a feasible connection and no traveller would consider
# it, so including such options flatters the method: the penalty gets to "avoid" hubs
# that were never real candidates.


def _risk_before(state, ap, hour):
    """Observed late share at this airport in the hour before -- as-of safe."""
    prev = hour - dt.timedelta(hours=1)
    v = state.get((ap, prev))
    return v["late_share"] if v else None


def build_itineraries(flights, state, communities_by_hour, origin, dest, after_hour,
                      xy=None, same_carrier=True):
    """Direct and one-stop options leaving `origin` after `after_hour`.

    `after_hour` is also the DECISION time. Every observed quantity is read as of the
    last complete hour before it (A24): an earlier version read each leg's risk from
    the hour before that leg departed, which for a second leg or a later flight is
    information the traveller does not yet have.

    `same_carrier`: both legs of a connection on one operating carrier (S6). Mixing
    carriers produced bookings like UA + WN that no single ticket would contain.
    """
    xy = xy if xy is not None else coords()
    direct_km = (great_circle_km(origin, dest, xy)
                 if origin in xy and dest in xy else None)
    majors = [f for f in flights if f["carrier"] in MAJORS]
    out_o = [f for f in majors if f["origin"] == origin and f["dep_utc"] >= after_hour]
    into_d = [f for f in majors if f["dest"] == dest and f["dep_utc"] >= after_hour]

    options = []
    for f in out_o:
        if f["dest"] == dest:
            options.append({"legs": [f], "hub": None})
    for f in out_o:
        if f["dest"] == dest:
            continue
        for g in into_d:
            if g["origin"] != f["dest"] or g["dep_utc"] <= f["arr_utc"]:
                continue
            if same_carrier and g["carrier"] != f["carrier"]:
                continue
            gap = (g["dep_utc"] - f["arr_utc"]).total_seconds() / 60.0
            if not (MCT_MIN <= gap <= MAX_CONNECT_MIN):
                continue
            hub = f["dest"]
            if direct_km and hub in xy:
                via = (great_circle_km(origin, hub, xy)
                       + great_circle_km(hub, dest, xy))
                if via > MAX_DETOUR * direct_km:
                    continue
            options.append({"legs": [f, g], "hub": hub})

    scored = []
    for o in options:
        risks = [_risk_before(state, l["origin"], min(l["sched_dep_hour"], after_hour))
                 for l in o["legs"]]
        if any(r is None for r in risks):
            continue
        base = float(np.mean(risks))

        # same-community penalty: does the hub share the origin's problem?
        #
        # The community must be read from the hour BEFORE the leg is scheduled, for
        # the same reason as `_risk_before`. Communities are keyed by the hour flights
        # ACTUALLY departed in, so the community for hour h is not complete until h
        # ends -- after the traveller has had to decide. Reading it by scheduled hour
        # was a leak of exactly the kind the STGNN work found (T11).
        same_comm = False
        h = min(o["legs"][0]["sched_dep_hour"], after_hour) - dt.timedelta(hours=1)
        comm = communities_by_hour.get(h, {})
        if o["hub"] and origin in comm and o["hub"] in comm:
            same_comm = comm[origin] == comm[o["hub"]]

        held = _itinerary_held(o["legs"])
        scored.append({"legs": o["legs"], "hub": o["hub"], "base": base,
                       "community": base + PENALTY * same_comm,
                       "same_comm": same_comm, "held": held,
                       "n_legs": len(o["legs"])})
    return scored


def _itinerary_held(legs) -> bool:
    """Ground truth: every leg operated, and the connection was physically makeable."""
    for l in legs:
        if l["cancelled"] or l["dep_delay"] is None or l["arr_delay"] is None:
            return False
    for a, b in zip(legs, legs[1:]):
        actual_arr = a["arr_utc"] + dt.timedelta(minutes=float(a["arr_delay"]))
        actual_dep = b["dep_utc"] + dt.timedelta(minutes=float(b["dep_delay"]))
        if (actual_dep - actual_arr).total_seconds() / 60.0 < MCT_MIN:
            return False
    return True


def evaluate_routing(flights, state, communities_by_hour, cases, xy=None):
    """Top-1 success rate, baseline vs community-aware, over routing cases."""
    xy = xy if xy is not None else coords()
    rows = []
    for origin, dest, after_hour in cases:
        opts = build_itineraries(flights, state, communities_by_hour,
                                 origin, dest, after_hour, xy)
        if len(opts) < 3:
            continue
        if not any(o["hub"] for o in opts):
            continue                      # no connection to reason about
        b = min(opts, key=lambda o: (o["base"], o["n_legs"]))
        c = min(opts, key=lambda o: (o["community"], o["n_legs"]))
        rows.append({"origin": origin, "dest": dest, "hour": after_hour,
                     "n_options": len(opts),
                     "baseline_held": b["held"], "community_held": c["held"],
                     "changed": b["legs"] != c["legs"],
                     "best_possible": any(o["held"] for o in opts)})
    return rows
