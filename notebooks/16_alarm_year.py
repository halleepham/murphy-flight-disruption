"""How often does the abnormality alarm fire across all of 2024? -- Challenge 5.

Runs the same rule as the app: an hour alarms when at least 25% of the airports scored
that hour are flagged, and only hours with at least 30 scored airports count. It also
reports the looser rule in anomaly.lead_time, which has no minimum.

This is a count, not an accuracy measure. Nothing here says which alarm days were
truly disrupted. Like the other anomaly results, it scores flights from every carrier
in the Parquet table.

Writes results/alarm_2024.json.

Run:  PYTHONPATH=src .venv/bin/python notebooks/16_alarm_year.py
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb

from murphy import anomaly as A

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "alarm_2024.json"
ALARM_SHARE = 0.25   # codesign_export.ALARM_SHARE
MIN_SCORED = 30      # codesign_export.MIN_SCORED


def main() -> None:
    con = duckdb.connect()
    base = A.fit_baseline(con)
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(366)]
    app_rule, no_minimum = [], []
    for day in days:
        signal = A.national_signal(A.score_day(day.isoformat(), base, con))
        alarms = [(h, s) for h, s in sorted(signal.items())
                  if s["share_flagged"] >= ALARM_SHARE]
        if alarms:
            no_minimum.append(day.isoformat())
        solid = [(h, s) for h, s in alarms if s["scored"] >= MIN_SCORED]
        if solid:
            hour, s = solid[0]
            app_rule.append({
                "day": day.isoformat(),
                "first_alarm_utc": f"{hour.astimezone(dt.timezone.utc):%H}:00Z",
                "share_flagged": round(s["share_flagged"], 4),
                "scored": s["scored"],
            })
    res = {
        "config": {"alarm_share": ALARM_SHARE, "min_scored": MIN_SCORED,
                   "baseline_year": A.BASELINE_YEAR, "carriers": "all in the table"},
        "days_scored": len(days),
        "alarm_days_app_rule": len(app_rule),
        "alarm_days_no_minimum": len(no_minimum),
        "alarms": app_rule,
    }
    OUT.write_text(json.dumps(res, indent=2))
    print(f"{len(app_rule)} of {len(days)} days alarm under the app's rule; "
          f"{len(no_minimum)} with no minimum. Wrote {OUT.name}")


if __name__ == "__main__":
    main()
