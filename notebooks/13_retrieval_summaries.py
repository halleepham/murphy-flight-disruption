"""Does community-aware retrieval beat returning rows? -- Challenge 4, AI Design.

Stage 1 promised "retrieve the group and its summary, not rows". Stage 2 built a
ranking penalty instead and left the retrieval half untested. This tests it, and
builds the summaries.

THE TASK, as a traveller would pose it
--------------------------------------
Standing at DEN at hour h: **which airports will be disrupted at h+2, so I know what
to avoid connecting through?** Forward-looking, so neither method can simply look the
answer up, and both see only as-of information.

  BASELINE          return the k airports with the highest observed late share right
                    now. This is what row retrieval does today.
  COMMUNITY-AWARE   return the members of the traveller's own community right now.

  GROUND TRUTH      airports whose failure rate at h+2 is >= 0.5.

k is set to the community's size each hour, so neither method is handed an advantage
in how much it may return.

TWO QUERY KINDS, which is the distinction the reference package draws
---------------------------------------------------------------------
  GLOBAL     "what should I avoid?" -- a set-valued question. Communities should help.
  TARGETED   "how bad is DEN specifically?" -- a one-airport question. Communities can
             only dilute it, and the size of that dilution is worth measuring rather
             than asserting.

Writes results/retrieval.json and results/community_summaries.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/13_retrieval_summaries.py
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

from murphy import anomaly as A
from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "retrieval.json"
SUMM = pathlib.Path(__file__).resolve().parents[1] / "results" / "community_summaries.json"
HORIZON = 2
BAD = 0.5


def failure_rate(flights) -> dict:
    agg = collections.defaultdict(lambda: [0, 0])
    for f in flights:
        if f["dep_delay"] is None and not f["cancelled"]:
            continue
        k = (f["origin"], f["actual_dep_hour"])
        agg[k][0] += 1
        agg[k][1] += int(f["failed"])
    return {k: v[1] / v[0] for k, v in agg.items()
            if v[0] >= C.MIN_FLIGHTS_HOUR}


def prf(pred: set, truth: set) -> dict:
    tp = len(pred & truth)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(truth) if truth else float("nan")
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def summarize(name, members, state, rate, anom, hour) -> dict:
    """A community summary. Every number computed; nothing generated as a value.

    This is the text GraphRAG would retrieve instead of rows. The project rule stands:
    the language may be generated, the numbers never are.
    """
    late = [state[(a, hour)]["late_share"] for a in members if (a, hour) in state]
    flights = sum(state[(a, hour)]["n"] for a in members if (a, hour) in state)
    flagged = [a for a in members if anom.get((a, hour), False)]
    biggest = sorted([a for a in members if (a, hour) in state],
                     key=lambda a: -state[(a, hour)]["n"])[:4]
    share = float(np.mean(late)) if late else float("nan")
    return {
        "community": name,
        "hour": f"{hour:%H}:00Z",
        "airports": len(members),
        "flights": flights,
        "mean_late_share": round(share, 3),
        "abnormal_airports": len(flagged),
        "text": (f"{len(members)} airports including "
                 f"{', '.join(biggest)} — {share:.0%} of departures late across "
                 f"{flights} flights this hour"
                 + (f"; {len(flagged)} running abnormally for the time of day."
                    if flagged else ".")),
        "members": sorted(members),
    }


def main() -> None:
    con = duckdb.connect()
    xy = C.coords()
    base = A.fit_baseline(con)
    res, summaries = {"horizon_hours": HORIZON, "bad_threshold": BAD}, {}

    for label, day in (("storm", C.STORM_DAY), ("calm", C.CALM_DAY)):
        d = C.load_day(day, con)
        state = C.airport_hour_state(d["flights"])
        rate = failure_rate(d["flights"])
        anom = {(r["airport"], r["hour"]): r["flag"]
                for r in A.score_day(day, base, con)}

        comm = {}
        for h in sorted({h for _, h in state}):
            g = C.build_airport_graph(state, h, xy)
            if g.vcount() >= 5:
                comm[h] = dict(zip(g.vs["name"], C.detect(g, "leiden")))

        glob_b, glob_c, targ_b, targ_c, sizes = [], [], [], [], []
        for h, memb in comm.items():
            future = h + dt.timedelta(hours=HORIZON)
            truth = {a for a in memb if rate.get((a, future), 0) >= BAD}
            if len(truth) < 3:
                continue
            for seed in memb:
                group = {a for a in memb if memb[a] == memb[seed]}
                if len(group) < 3:
                    continue
                k = len(group)
                ranked = sorted((a for a in memb if (a, h) in state),
                                key=lambda a: -state[(a, h)]["late_share"])[:k]
                glob_b.append(prf(set(ranked), truth))
                glob_c.append(prf(group, truth))
                sizes.append(k)
                # targeted: the question is about `seed` alone
                targ_b.append(prf({seed}, {seed}))
                targ_c.append(prf(group, {seed}))

        agg = lambda rows, key: round(float(np.mean([r[key] for r in rows])), 4)
        rec = {
            "day": day, "queries": len(glob_b),
            "mean_returned": round(float(np.mean(sizes)), 1),
            "global": {
                "baseline": {k: agg(glob_b, k) for k in ("precision", "recall", "f1")},
                "community": {k: agg(glob_c, k) for k in ("precision", "recall", "f1")},
            },
            "targeted": {
                "baseline_precision": agg(targ_b, "precision"),
                "community_precision": agg(targ_c, "precision"),
            },
        }
        res[label] = rec
        gb, gc = rec["global"]["baseline"], rec["global"]["community"]
        print(f"\n=== {label} {day} ===  {rec['queries']:,} queries, "
              f"returning {rec['mean_returned']:.1f} airports each")
        print(f"  GLOBAL  'which airports will be disrupted in {HORIZON}h?'")
        print(f"    rows (baseline)   P {gb['precision']:.3f}  R {gb['recall']:.3f}  "
              f"F1 {gb['f1']:.3f}")
        print(f"    community-aware   P {gc['precision']:.3f}  R {gc['recall']:.3f}  "
              f"F1 {gc['f1']:.3f}   ({gc['f1'] - gb['f1']:+.3f})")
        print(f"  TARGETED  'how bad is THIS airport?'")
        print(f"    rows precision {rec['targeted']['baseline_precision']:.3f}   "
              f"community precision {rec['targeted']['community_precision']:.3f}   "
              f"-> dilution "
              f"{rec['targeted']['baseline_precision'] - rec['targeted']['community_precision']:+.3f}")

        # --- summaries for one representative hour ------------------------------
        peak = max(comm, key=lambda h: len(comm[h]))
        memb = comm[peak]
        groups = collections.defaultdict(set)
        for a, cid in memb.items():
            groups[cid].add(a)
        summaries[label] = {
            "hour": f"{peak:%Y-%m-%d %H}:00Z",
            "communities": [summarize(cid, mem, state, rate, anom, peak)
                            for cid, mem in sorted(groups.items())
                            if len(mem) >= 3],
        }
        rows_needed = sum(len(m) for m in groups.values() if len(m) >= 3)
        n_sum = len(summaries[label]["communities"])
        rec["summary_compression"] = {
            "airport_rows": rows_needed, "summaries": n_sum,
            "ratio": round(rows_needed / n_sum, 1)}
        print(f"  SUMMARIES at {peak:%H}:00Z — {n_sum} summaries replace "
              f"{rows_needed} airport rows ({rows_needed / n_sum:.1f}x)")
        for s in summaries[label]["communities"][:3]:
            print(f"     [{s['community']}] {s['text']}")

    OUT.write_text(json.dumps(res, indent=2))
    SUMM.write_text(json.dumps(summaries, indent=2))
    print(f"\nwritten {OUT.name}, {SUMM.name}")


if __name__ == "__main__":
    main()
