"""C3 — can one detector find whichever structure a day actually has?

The two bad days differ in kind. On 15 January, knowing the AIRPORT explains who
failed (13.5% against carrier's 3.7%). On 19 July it is the CARRIER (28.0% against
14.0%), because CrowdStrike hit airlines, and an airline's hubs are scattered.

A geographic community can only ever express the first. So instead of two detectors,
one graph whose nodes carry both facts:

  NODE   a (carrier, airport) unit -- "Delta at Atlanta", "Southwest at Midway"
  EDGE   same carrier, different airport   ->  an airline-wide coupling
         same airport, different carrier   ->  a local coupling
         both weighted by how alike the two units are actually behaving

Neither edge type is given a head start: the base weights are equal and behaviour
decides. So the community structure that comes out is a statement about the day.

Diagnostic: is the result carrier-shaped or airport-shaped? Measured with normalised
mutual information against each label set.

Writes results/carrier_communities.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/15_carrier_communities.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import igraph as ig
import numpy as np
from sklearn.metrics import normalized_mutual_info_score as nmi

from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "carrier_communities.json"
MIN_UNIT_FLIGHTS = 6


def unit_state(flights):
    """Failure share per (carrier, airport) for the day."""
    agg = collections.defaultdict(lambda: [0, 0])
    for f in flights:
        if f["carrier"] not in C.MAJORS:
            continue
        if f["dep_delay"] is None and not f["cancelled"]:
            continue
        k = (f["carrier"], f["origin"])
        agg[k][0] += 1
        agg[k][1] += int(f["failed"])
    return {k: {"n": v[0], "fail": v[1] / v[0]}
            for k, v in agg.items() if v[0] >= MIN_UNIT_FLIGHTS}


def build(units, xy):
    """One graph, two edge types, each normalised so neither can win by counting.

    Two corrections, both found by the first run coming out degenerate -- every day
    "carrier-shaped", every largest community simply all of Southwest:

      1. A carrier spans ~90 airports, an airport hosts ~9 carriers, so same-carrier
         edges outnumber same-airport edges roughly 100 to 1. Left alone, the
         algorithm recovers the carriers no matter what the day looked like. Each
         edge type is therefore scaled to carry the SAME TOTAL weight.
      2. "Alike" as 1 - |difference| is near 1 for almost every pair on a calm day,
         when nothing is failing. Similarity is now measured against the spread of
         THAT DAY, so it means "unusually alike for today" rather than "both small".
    """
    keys = sorted(units)
    idx = {k: i for i, k in enumerate(keys)}
    g = ig.Graph(); g.add_vertices(len(keys))
    g.vs["name"] = [f"{c}@{a}" for c, a in keys]
    g.vs["carrier"] = [c for c, _ in keys]
    g.vs["airport"] = [a for _, a in keys]

    spread = float(np.std([u["fail"] for u in units.values()])) or 0.05
    edges, w, kind = [], [], []
    for i, (c1, a1) in enumerate(keys):
        for j in range(i + 1, len(keys)):
            c2, a2 = keys[j]
            if c1 == c2 and a1 != a2:
                k = "carrier"
            elif a1 == a2 and c1 != c2:
                k = "airport"
            else:
                continue
            diff = abs(units[(c1, a1)]["fail"] - units[(c2, a2)]["fail"])
            val = float(np.exp(-((diff / spread) ** 2)))   # alike FOR TODAY
            if val > 0.05:
                edges.append((i, j)); w.append(val); kind.append(k)

    # equal total weight per edge type, so neither mechanism wins by having more edges
    tot = {"carrier": sum(v for v, k in zip(w, kind) if k == "carrier") or 1.0,
           "airport": sum(v for v, k in zip(w, kind) if k == "airport") or 1.0}
    w = [v / tot[k] for v, k in zip(w, kind)]

    g.add_edges(edges); g.es["weight"] = w; g.es["kind"] = kind
    return g


def main() -> None:
    con = duckdb.connect()
    xy = C.coords()
    res = {"min_unit_flights": MIN_UNIT_FLIGHTS}

    for label, day in (("weather", C.WEATHER_DAY), ("storm", C.STORM_DAY),
                       ("calm", C.CALM_DAY)):
        d = C.load_day(day, con)
        units = unit_state(d["flights"])
        g = build(units, xy)
        mem = C.detect(g, "leiden", seed=42)

        carrier_nmi = float(nmi(g.vs["carrier"], mem))
        airport_nmi = float(nmi(g.vs["airport"], mem))
        sizes = collections.Counter(mem)

        # what does the biggest community look like?
        big = sizes.most_common(1)[0][0]
        members = [(g.vs[i]["carrier"], g.vs[i]["airport"])
                   for i, m in enumerate(mem) if m == big]
        carr_mix = collections.Counter(c for c, _ in members).most_common(3)
        fails = [units[(c, a)]["fail"] for c, a in members]

        kinds = collections.Counter(g.es["kind"])
        rec = {
            "day": day, "units": g.vcount(), "edges": g.ecount(),
            "carrier_edges": kinds["carrier"], "airport_edges": kinds["airport"],
            "communities": len(sizes),
            "nmi_carrier": round(carrier_nmi, 4),
            "nmi_airport": round(airport_nmi, 4),
            "shape": ("carrier-shaped" if carrier_nmi > airport_nmi * 1.15
                      else "airport-shaped" if airport_nmi > carrier_nmi * 1.15
                      else "mixed"),
            "largest_community": {
                "units": len(members),
                "mean_failure": round(float(np.mean(fails)), 3),
                "top_carriers": [[c, n] for c, n in carr_mix],
            },
        }
        res[label] = rec
        print(f"\n=== {label}  {day} ===")
        print(f"  {g.vcount()} carrier-airport units, {g.ecount():,} edges "
              f"({kinds['carrier']:,} carrier / {kinds['airport']:,} airport, "
              f"equal total weight), {len(sizes)} communities")
        print(f"  aligned with CARRIER  {carrier_nmi:.3f}")
        print(f"  aligned with AIRPORT  {airport_nmi:.3f}")
        print(f"  -> {rec['shape'].upper()}")
        print(f"  largest community: {len(members)} units, "
              f"{rec['largest_community']['mean_failure']:.0%} failed, "
              f"mostly {', '.join(f'{c}({n})' for c, n in carr_mix)}")

    OUT.write_text(json.dumps(res, indent=2))
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
