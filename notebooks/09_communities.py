"""Does community detection actually help? -- Challenge 4, AI Design.

Stage 1 claimed communities would make correlated failure visible and fix switching
decisions. This runs that claim against two real days and reports where it holds.

  storm  19 Jul 2024, the CrowdStrike outage -- 52.1% of departures 15+ late, 16.1%
         cancelled
  calm   29 Oct 2024 -- 8.2% late, 0.18% cancelled

Outputs results/communities.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/09_communities.py
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np

from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "communities.json"


def communities_per_hour(state, xy, method="leiden"):
    """C1 membership for every hour of the day, plus per-hour graph stats."""
    out, stats = {}, {}
    for h in sorted({h for _, h in state}):
        g = C.build_airport_graph(state, h, xy)
        if g.vcount() < 5:
            continue
        mem = C.detect(g, method)
        out[h] = dict(zip(g.vs["name"], mem))
        ok, tot = C.connected_communities(g, mem)
        stats[h] = {"airports": g.vcount(), "edges": g.ecount(),
                    "communities": len(set(mem)),
                    "modularity": round(C.modularity(g, mem), 4),
                    "connected": f"{ok}/{tot}"}
    return out, stats


def _shuffled(comm: dict, rng) -> dict:
    """Same community sizes, membership randomised -- the null for both metrics below."""
    labels = list(comm.values())
    rng.shuffle(labels)
    return dict(zip(comm.keys(), labels))


def stability(comm_by_hour) -> float:
    """Do airport pairs keep the same together/apart relation in the next hour?

    A community that reshuffles every hour gives a traveller contradictory advice, so
    this is a precondition for using them at all. Stage 1 never named it.
    """
    hours = sorted(comm_by_hour)
    agree = total = 0
    for h1, h2 in zip(hours, hours[1:]):
        if (h2 - h1) != dt.timedelta(hours=1):
            continue
        a, b = comm_by_hour[h1], comm_by_hour[h2]
        shared = sorted(set(a) & set(b))
        for i in range(len(shared)):
            for j in range(i + 1, len(shared)):
                x, y = shared[i], shared[j]
                total += 1
                agree += int((a[x] == a[y]) == (b[x] == b[y]))
    return agree / total if total else float("nan")


def stability_null(comm_by_hour, seed=42) -> float:
    """Stability when the second hour's labels are shuffled.

    Needed because most airport pairs sit in DIFFERENT communities in both hours, so
    they agree trivially. Without this control a high score means nothing.
    """
    rng = np.random.default_rng(seed)
    shuffled = {h: _shuffled(c, rng) for h, c in comm_by_hour.items()}
    hours = sorted(comm_by_hour)
    agree = total = 0
    for h1, h2 in zip(hours, hours[1:]):
        if (h2 - h1) != dt.timedelta(hours=1):
            continue
        a, b = comm_by_hour[h1], shuffled[h2]
        shared = sorted(set(a) & set(b))
        for i in range(len(shared)):
            for j in range(i + 1, len(shared)):
                x, y = shared[i], shared[j]
                total += 1
                agree += int((a[x] == a[y]) == (b[x] == b[y]))
    return agree / total if total else float("nan")


def cofailure(flights, comm_by_hour, seed=42):
    """The Stage 1 claim at its simplest: do same-community airports fail together?

    For every hour, every pair of airports present: did both have a high failure rate?
    Reported separately for same-community and different-community pairs.
    """
    fail = collections.defaultdict(lambda: [0, 0])
    for f in flights:
        if f["dep_delay"] is None and not f["cancelled"]:
            continue
        # Keyed by actual departure hour, matching how communities are keyed, so the
        # two sides of the comparison describe the same hour.
        k = (f["origin"], f["actual_dep_hour"])
        fail[k][0] += 1
        fail[k][1] += int(f["failed"])
    rate = {k: v[1] / v[0] for k, v in fail.items() if v[0] >= C.MIN_FLIGHTS_HOUR}

    rng = np.random.default_rng(seed)
    same, diff, null_same, null_diff = [], [], [], []
    for h, comm in comm_by_hour.items():
        aps = [a for a in comm if (a, h) in rate]
        shuf = _shuffled({a: comm[a] for a in aps}, rng)
        for i in range(len(aps)):
            for j in range(i + 1, len(aps)):
                x, y = aps[i], aps[j]
                both_bad = int(rate[(x, h)] >= 0.5 and rate[(y, h)] >= 0.5)
                (same if comm[x] == comm[y] else diff).append(both_bad)
                (null_same if shuf[x] == shuf[y] else null_diff).append(both_bad)
    m = lambda v: round(float(np.mean(v)), 4) if v else None
    return {"same_community_pairs": len(same), "same_community_both_bad": m(same),
            "diff_community_pairs": len(diff), "diff_community_both_bad": m(diff),
            "lift": round(m(same) - m(diff), 4) if same and diff else None,
            "null_same_both_bad": m(null_same), "null_diff_both_bad": m(null_diff),
            "null_lift": round(m(null_same) - m(null_diff), 4)
                         if null_same and null_diff else None}


def flight_graph_algorithms(flights, seeds=(7, 13, 29, 42, 101)):
    """Leiden vs Louvain on the C2 graph, across seeds.

    The Stage 1 reason for choosing Leiden was its guarantee that every community is
    internally connected. The airport graph is too small and dense to test it. The
    flight graph -- ~21k nodes, ~48k edges -- is where it shows.
    """
    g = C.build_flight_graph(flights)
    out = {"nodes": g.vcount(), "edges": g.ecount()}
    for method in ("leiden", "louvain"):
        comms, mods, bad = [], [], 0
        for sd in seeds:
            mem = C.detect(g, method, seed=sd)
            ok, tot = C.connected_communities(g, mem)
            comms.append(len(set(mem)))
            mods.append(C.modularity(g, mem))
            bad += int(ok != tot)
        out[method] = {"seeds": len(seeds),
                       "communities_mean": round(float(np.mean(comms)), 1),
                       "communities_std": round(float(np.std(comms)), 2),
                       "modularity_mean": round(float(np.mean(mods)), 4),
                       "runs_with_disconnected_community": bad}
    return out


def routing_cases(flights, state, limit_pairs=40):
    """Busy origin-destination pairs among the nine majors, one case per hour block."""
    majors = [f for f in flights if f["carrier"] in C.MAJORS]
    pairs = collections.Counter((f["origin"], f["dest"]) for f in majors)
    top = [p for p, n in pairs.most_common(limit_pairs) if n >= 3]
    hours = sorted({f["sched_dep_hour"] for f in majors})
    cases = []
    for (o, d) in top:
        for h in hours[::3]:
            cases.append((o, d, h))
    return cases


def main() -> None:
    xy = C.coords()
    con = duckdb.connect()
    results = {"config": {"geo_sigma_km": C.GEO_SIGMA_KM, "mct_min": C.MCT_MIN,
                          "penalty": C.PENALTY, "late_min": C.LATE_MIN}}

    for label, day in (("storm", C.STORM_DAY), ("calm", C.CALM_DAY)):
        print(f"\n=== {label}  {day} ===")
        d = C.load_day(day, con)
        state = C.airport_hour_state(d["flights"])
        rec: dict = {"day": day}

        # --- algorithm comparison, on the real graph -----------------------------
        algo = {}
        for method in ("leiden", "louvain"):
            comm, stats = communities_per_hour(state, xy, method)
            mods = [s["modularity"] for s in stats.values()]
            bad = sum(1 for s in stats.values()
                      if s["connected"].split("/")[0] != s["connected"].split("/")[1])
            algo[method] = {"hours": len(stats),
                            "mean_communities": round(float(np.mean(
                                [s["communities"] for s in stats.values()])), 2),
                            "mean_modularity": round(float(np.mean(mods)), 4),
                            "hours_with_disconnected_community": bad,
                            "stability": round(stability(comm), 4),
                            "stability_null": round(stability_null(comm), 4)}
            print(f"  {method:<8} {algo[method]['mean_communities']:>5} communities/hour  "
                  f"modularity {algo[method]['mean_modularity']:.3f}  "
                  f"stability {algo[method]['stability']:.3f} "
                  f"(null {algo[method]['stability_null']:.3f})  "
                  f"disconnected-hours {bad}")
            if method == "leiden":
                comm_leiden, stats_leiden = comm, stats
        rec["algorithms"] = algo

        # --- does the claim hold? -------------------------------------------------
        rec["cofailure"] = cofailure(d["flights"], comm_leiden)
        cf = rec["cofailure"]
        print(f"  co-failure  same {cf['same_community_both_bad']} vs diff "
              f"{cf['diff_community_both_bad']}  lift {cf['lift']:+.4f}   "
              f"| shuffled null lift {cf['null_lift']:+.4f}")

        # --- Leiden's guarantee, tested where it can show ------------------------
        rec["flight_graph"] = flight_graph_algorithms(d["flights"])
        fg = rec["flight_graph"]
        print(f"  C2 graph    {fg['nodes']:,} nodes {fg['edges']:,} edges")
        for m in ("leiden", "louvain"):
            v = fg[m]
            print(f"     {m:<8} {v['communities_mean']:>6} +/- {v['communities_std']:<5} "
                  f"communities   modularity {v['modularity_mean']:.4f}   "
                  f"runs with a disconnected community: "
                  f"{v['runs_with_disconnected_community']}/{v['seeds']}")

        # --- snapshot for the figures --------------------------------------------
        peak = max(comm_leiden, key=lambda h: len(comm_leiden[h]))
        snap_state = {ap: v for (ap, h), v in state.items() if h == peak}
        rec["snapshot"] = {
            "hour": f"{peak:%Y-%m-%d %H:00}Z",
            "airports": {ap: {"community": comm_leiden[peak][ap],
                              "lat": xy[ap][0], "lon": xy[ap][1],
                              "late_share": round(snap_state[ap]["late_share"], 4),
                              "n": snap_state[ap]["n"]}
                         for ap in comm_leiden[peak] if ap in snap_state and ap in xy},
        }
        print(f"  snapshot    {peak:%H}:00Z  "
              f"{len(rec['snapshot']['airports'])} airports  "
              f"{len(set(comm_leiden[peak].values()))} communities")

        # --- the routing decision -------------------------------------------------
        cases = routing_cases(d["flights"], state)
        rows = C.evaluate_routing(d["flights"], state, comm_leiden, cases)
        if rows:
            b = float(np.mean([r["baseline_held"] for r in rows]))
            c = float(np.mean([r["community_held"] for r in rows]))
            oracle = float(np.mean([r["best_possible"] for r in rows]))
            changed = [r for r in rows if r["changed"]]
            ch_b = float(np.mean([r["baseline_held"] for r in changed])) if changed else None
            ch_c = float(np.mean([r["community_held"] for r in changed])) if changed else None
            better = sum(1 for r in changed
                         if r["community_held"] and not r["baseline_held"])
            worse = sum(1 for r in changed
                        if r["baseline_held"] and not r["community_held"])
            neutral = len(changed) - better - worse
            rec["routing"] = {
                "cases": len(rows), "baseline_top1_held": round(b, 4),
                "community_top1_held": round(c, 4), "oracle_any_held": round(oracle, 4),
                "cases_changed": len(changed),
                "cases_unchanged": len(rows) - len(changed),
                "changed_baseline_held": round(ch_b, 4) if ch_b is not None else None,
                "changed_community_held": round(ch_c, 4) if ch_c is not None else None,
                "changed_better": better, "changed_worse": worse,
                "changed_neutral": neutral,
            }
            print(f"  routing     {len(rows)} cases   baseline {b:.3f}  "
                  f"community {c:.3f}  oracle {oracle:.3f}   "
                  f"changed {len(changed)}")
            if changed:
                print(f"              on the {len(changed)} changed: "
                      f"baseline {ch_b:.3f} -> community {ch_c:.3f}   "
                      f"[better {better} · worse {worse} · neutral {neutral}]")
                print(f"              unchanged (no effect): "
                      f"{len(rows) - len(changed)} of {len(rows)}")
        # --- is the gain an artefact of one arbitrary constant? -------------------
        sweep = {}
        for pen in (0.0, 0.05, 0.10, 0.25, 0.50, 1.00):
            C.PENALTY = pen
            rr = C.evaluate_routing(d["flights"], state, comm_leiden, cases)
            if rr:
                sweep[f"{pen:.2f}"] = {
                    "top1_held": round(float(np.mean([r["community_held"] for r in rr])), 4),
                    "changed": sum(1 for r in rr if r["changed"])}
        C.PENALTY = 0.25
        rec["penalty_sweep"] = sweep
        print("  penalty     " + "  ".join(
            f"{k}:{v['top1_held']:.3f}({v['changed']})" for k, v in sweep.items()))

        results[label] = rec

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
