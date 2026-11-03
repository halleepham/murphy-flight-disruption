"""Anomaly detection on airport-hours: does it warn, or only describe? -- Challenge 4.

Baselines from 2023, scored on two 2024 days. Writes results/anomaly.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/10_anomaly.py
"""

from __future__ import annotations

import datetime as _dt
UTC = _dt.timezone.utc  # DuckDB returns session-zone (Central) instants; label in UTC (A25)
import json
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np

from murphy import anomaly as A

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "anomaly.json"


def main() -> None:
    con = duckdb.connect()
    base = A.fit_baseline(con)
    res = {"config": {"baseline_year": A.BASELINE_YEAR, "z_flag": A.Z_FLAG,
                      "min_flights": A.MIN_FLIGHTS},
           "baseline_slots": len(base)}
    print(f"2023 baselines: {len(base):,} (airport, hour-of-day) slots")

    for label, day in (("storm", "2024-07-19"), ("calm", "2024-10-29")):
        rows = A.score_day(day, base, con)
        iso = A.isolation_forest(rows, base)
        sig = A.national_signal(rows)
        lt = A.lead_time(sig)
        rec = {
            "day": day,
            "airport_hours_scored": len(rows),
            "robust_z": {
                "flagged": sum(r["flag"] for r in rows),
                "share_flagged": round(sum(r["flag"] for r in rows) / len(rows), 4),
                "median_z": round(float(np.median([r["z"] for r in rows])), 3),
            },
            "isolation_forest": {
                "flagged": int(sum(iso)),
                "share_flagged": round(sum(iso) / len(iso), 4),
            },
            "lead_time": lt,
            # A list, in true chronological order. Keying by "%H" collapsed the
            # UTC day boundary -- a storm day runs 05:00Z to 02:00Z the next day, and
            # string-sorting put those closing hours at the front of the chart.
            "hourly": [{"hour": f"{h.astimezone(UTC):%Y-%m-%d %H}", "label": f"{h.astimezone(UTC):%H}",
                        "scored": v["scored"], "flagged": v["flagged"],
                        "share_flagged": round(v["share_flagged"], 4),
                        "mean_late": round(v["mean_late"], 4)}
                       for h, v in sorted(sig.items())],
            "top_anomalies": [
                {"airport": r["airport"], "hour": f"{r['hour'].astimezone(UTC):%H}:00Z",
                 "late_share": round(r["late_share"], 3),
                 "usual": round(r["median"], 3), "z": round(r["z"], 1), "n": r["n"]}
                for r in sorted(rows, key=lambda r: -r["z"])[:8]],
        }
        res[label] = rec
        print(f"\n{label} {day}: {len(rows):,} scored, "
              f"{rec['robust_z']['share_flagged']:.1%} flagged "
              f"(isolation forest {rec['isolation_forest']['share_flagged']:.1%})")
        print(f"  alarm first fires {lt['first_alarm']}, "
              f"lateness peaks {lt['peak_lateness']}, "
              f"lead {lt['lead_hours']} h" if lt["first_alarm"]
              else "  alarm never fires")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, default=str))
    print(f"\nwritten {OUT.name}")


if __name__ == "__main__":
    main()
