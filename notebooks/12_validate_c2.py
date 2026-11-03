"""Does C2 earn its place? -- Challenge 4, AI Design.

C2 (rotation-coupled flights) is drawn in the Stage 1 figure and built in code, and so
far no result depends on it. This tests it the same way C1 was tested, with the control
that matters.

**The control.** Flights sharing an airport-hour are in the same C2 community BY
CONSTRUCTION -- the airport-hour node is the bridge. So "same community implies
co-failure" is trivially true for flights in the same place at the same time, and
proves nothing. The question is whether C2 says anything about flights in
DIFFERENT airport-hours: does being chained to the same aircraft, or colliding at the
same hub earlier in the day, predict failing together?

Three comparisons, all on pairs of flights in different airport-hours:

  same C2 community      do they fail together more often?
  different C2 community the reference
  shuffled labels        the null, same community sizes, membership randomised

Writes results/c2_validation.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/12_validate_c2.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import random
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np

from murphy import communities as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "c2_validation.json"
MAX_PAIRS = 400_000
SEED = 42


def pair_stats(flights, member, rng, max_pairs=MAX_PAIRS):
    """Co-failure for flight pairs in DIFFERENT airport-hours, by C2 relation."""
    fl = [f for f in flights
          if f["flight_id"] in member and f["dep_delay"] is not None]
    idx = list(range(len(fl)))
    same, diff = [], []
    seen = 0
    while seen < max_pairs:
        i, j = rng.randrange(len(idx)), rng.randrange(len(idx))
        if i == j:
            continue
        a, b = fl[i], fl[j]
        # the control: never compare two flights from the same airport-hour
        if (a["origin"], a["actual_dep_hour"]) == (b["origin"], b["actual_dep_hour"]):
            continue
        seen += 1
        both = int(a["failed"] and b["failed"])
        (same if member[a["flight_id"]] == member[b["flight_id"]] else diff).append(both)
    return same, diff


def main() -> None:
    con = duckdb.connect()
    res = {"control": "pairs from the same airport-hour are excluded, because the "
                      "airport-hour node makes them same-community by construction"}

    for label, day in (("storm", C.STORM_DAY), ("calm", C.CALM_DAY)):
        d = C.load_day(day, con)
        g = C.build_flight_graph(d["flights"])
        mem = C.detect(g, "leiden", seed=SEED)
        member = {n: m for n, m, k in zip(g.vs["name"], mem, g.vs["kind"])
                  if k == "flight"}

        sizes = collections.Counter(member.values())
        rng = random.Random(SEED)
        same, diff = pair_stats(d["flights"], member, rng)

        # null: same community sizes, membership shuffled
        keys = list(member)
        vals = list(member.values())
        rng.shuffle(vals)
        shuffled = dict(zip(keys, vals))
        rng2 = random.Random(SEED)
        n_same, n_diff = pair_stats(d["flights"], shuffled, rng2)

        m = lambda v: round(float(np.mean(v)), 4) if v else None
        rec = {
            "day": day,
            "flights_in_graph": len(member),
            "communities": len(sizes),
            "largest_community_share": round(max(sizes.values()) / len(member), 4),
            "same_community_pairs": len(same), "same_community_both_fail": m(same),
            "diff_community_pairs": len(diff), "diff_community_both_fail": m(diff),
            "lift": round(m(same) - m(diff), 4),
            "null_lift": round(m(n_same) - m(n_diff), 4),
        }
        res[label] = rec
        print(f"\n=== {label} {day} ===")
        print(f"  {len(member):,} flights in {len(sizes)} C2 communities; "
              f"largest holds {rec['largest_community_share']:.1%}")
        print(f"  pairs in DIFFERENT airport-hours:")
        print(f"    same C2 community       {m(same):.4f} both fail  "
              f"({len(same):,} pairs)")
        print(f"    different C2 community  {m(diff):.4f} both fail  "
              f"({len(diff):,} pairs)")
        print(f"    lift {rec['lift']:+.4f}   |   shuffled null lift "
              f"{rec['null_lift']:+.4f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2))
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
