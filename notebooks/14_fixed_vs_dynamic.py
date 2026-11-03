"""Are the hourly communities doing anything a fixed map of regions would not?

The groups come out broadly regional on every day tested. If that is all they are,
then the hourly re-weighting is decoration and the honest claim shrinks to "nearby
airports fail together", which needed no algorithm.

So: the control.

  FIXED      run Leiden ONCE on the distance kernel alone, with no behaviour term.
             One map of regions, reused for every hour of every day.
  DYNAMIC    the hourly version -- distance re-weighted by how alike two airports
             are actually behaving that hour.

Both are then used identically in the routing task. If they score the same, the
grouping is geography and nothing more.

Writes results/fixed_vs_dynamic.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/14_fixed_vs_dynamic.py
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

from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "fixed_vs_dynamic.json"


def fixed_regions(airports, xy, seed=42):
    """Leiden on distance alone -- one map, no behaviour, never updated."""
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


def agreement(dyn_by_hour, fixed):
    """How often do the two groupings agree on whether a pair is together?"""
    same = tot = 0
    for h, memb in dyn_by_hour.items():
        aps = [a for a in memb if a in fixed]
        for i in range(len(aps)):
            for j in range(i + 1, len(aps)):
                x, y = aps[i], aps[j]
                tot += 1
                same += int((memb[x] == memb[y]) == (fixed[x] == fixed[y]))
    return same / tot if tot else float("nan")


def main() -> None:
    con = duckdb.connect()
    xy = C.coords()
    res = {}

    for label, day in (("weather", C.WEATHER_DAY), ("storm", C.STORM_DAY),
                       ("calm", C.CALM_DAY)):
        d = C.load_day(day, con)
        state = C.airport_hour_state(d["flights"])
        all_aps = sorted({a for a, _ in state})
        fixed = fixed_regions(all_aps, xy)

        dyn = {}
        for h in sorted({h for _, h in state}):
            g = C.build_airport_graph(state, h, xy)
            if g.vcount() >= 5:
                dyn[h] = dict(zip(g.vs["name"], C.detect(g, "leiden")))
        fixed_by_hour = {h: {a: fixed[a] for a in memb if a in fixed}
                         for h, memb in dyn.items()}

        majors = [f for f in d["flights"] if f["carrier"] in C.MAJORS]
        pairs = collections.Counter((f["origin"], f["dest"]) for f in majors)
        hrs = sorted({f["sched_dep_hour"] for f in majors})
        cases = [(o, dd, h) for (o, dd), _ in pairs.most_common(40) for h in hrs[::3]]

        out = {"day": day,
               "fixed_regions": len(set(fixed.values())),
               "agreement_fixed_vs_dynamic": round(agreement(dyn, fixed), 4)}
        for name, memb in (("dynamic", dyn), ("fixed", fixed_by_hour)):
            rows = C.evaluate_routing(d["flights"], state, memb, cases, xy)
            if not rows:
                continue
            ch = [r for r in rows if r["changed"]]
            out[name] = {
                "cases": len(rows),
                "top1_held": round(float(np.mean([r["community_held"] for r in rows])), 4),
                "changed": len(ch),
                "better": sum(1 for r in ch if r["community_held"] and not r["baseline_held"]),
                "worse": sum(1 for r in ch if r["baseline_held"] and not r["community_held"]),
            }
            out["baseline_top1_held"] = round(
                float(np.mean([r["baseline_held"] for r in rows])), 4)
        res[label] = out

        print(f"\n=== {label}  {day} ===")
        print(f"  fixed map has {out['fixed_regions']} regions; the two groupings agree "
              f"on {out['agreement_fixed_vs_dynamic']:.1%} of airport pairs")
        print(f"  baseline (no groups at all)  {out['baseline_top1_held']:.3f}")
        for name in ("fixed", "dynamic"):
            if name in out:
                v = out[name]
                print(f"  {name:<8} groups  {v['top1_held']:.3f}   "
                      f"changed {v['changed']:>3}  better {v['better']:>2}  "
                      f"worse {v['worse']:>2}")

    OUT.write_text(json.dumps(res, indent=2))
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
