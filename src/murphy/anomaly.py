"""Anomaly detection over airport-hours -- Challenge 4, AI Design.

**Why Murphy needs this and does not have it.** The shipped forecaster knows an
airport's climatology and nothing about today. It cannot say "this hour at ATL is
unlike anything in the training data", which is exactly the signal the trust layer is
supposed to carry. A delay forecast made in an unprecedented state should be
distrusted, and right now nothing measures that.

**The question is not "is this airport late".** Late share alone is already in the
data. The question is whether an airport-hour is late *in a way that is unusual for
that airport at that time of day* -- 40% late at ORD on a Friday evening is ordinary;
40% at 06:00 is not.

Method, in order of preference:

  ROBUST Z     per (airport, hour-of-day), take the 2023 distribution of late share
               and score 2024 against its median and MAD. Interpretable, provenance-
               friendly, and every flag can be explained to a traveller in one
               sentence. This is the one kept.

  ISOLATION    IsolationForest over the same features, as a check that a standard
  FOREST       ML detector does not find materially more. Reported, not shipped --
               a flag that cannot be explained cannot be shown to a user under
               Murphy's provenance rules.

Baselines come from 2023 only. Scoring is on 2024. The split is the usual one.

Run:  PYTHONPATH=src .venv/bin/python -m murphy.anomaly
"""

from __future__ import annotations

import datetime as dt
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
FLIGHTS = str(ROOT / "data" / "processed" / "flights_full" / "**" / "*.parquet")

LATE_MIN = 15
MIN_FLIGHTS = 3
BASELINE_YEAR = 2023
Z_FLAG = 3.0
MAD_SCALE = 1.4826      # makes MAD comparable to a standard deviation
MIN_MAD = 0.03          # floor, so a perfectly calm airport-hour cannot divide by ~0


def _panel_sql(where: str) -> str:
    """Late share per airport and ACTUAL departure hour -- the as-of safe keying."""
    return f"""
        SELECT origin AS ap,
               date_trunc('hour', dep_utc + INTERVAL (dep_delay_min) MINUTE) AS h,
               count(*) AS n,
               avg(CASE WHEN dep_delay_min >= {LATE_MIN} THEN 1.0 ELSE 0.0 END) AS late
        FROM read_parquet('{FLIGHTS}', hive_partitioning=true)
        WHERE cancelled = 0 AND dep_delay_min IS NOT NULL AND {where}
        GROUP BY 1, 2
        HAVING count(*) >= {MIN_FLIGHTS}
    """


def fit_baseline(con=None) -> dict:
    """Median and MAD of late share per (airport, hour of day), from 2023 alone."""
    con = con or duckdb.connect()
    rows = con.execute(_panel_sql(
        f"flight_date >= DATE '{BASELINE_YEAR}-01-01' "
        f"AND flight_date < DATE '{BASELINE_YEAR + 1}-01-01'")).fetchall()
    buckets: dict = {}
    for ap, h, _n, late in rows:
        buckets.setdefault((ap, h.hour), []).append(late)
    base = {}
    for key, vals in buckets.items():
        if len(vals) < 20:              # too few days to characterise this slot
            continue
        v = np.asarray(vals)
        med = float(np.median(v))
        mad = float(np.median(np.abs(v - med)))
        base[key] = {"median": med,
                     "scale": max(MAD_SCALE * mad, MIN_MAD),
                     "days": len(vals)}
    return base


def score_day(day: str, base: dict, con=None) -> list[dict]:
    """Robust z per airport-hour for one day, against that slot's own history."""
    con = con or duckdb.connect()
    rows = con.execute(_panel_sql(f"flight_date = DATE '{day}'")).fetchall()
    out = []
    for ap, h, n, late in rows:
        b = base.get((ap, h.hour))
        if not b:
            continue
        # Two sources of spread, not one. The historical MAD says how much this
        # slot normally varies; the binomial term says how much a share computed
        # from n flights wobbles by chance. Without the second term an hour with
        # three flights, all late, scores the same as a hub melting down.
        p0 = b["median"]
        sampling = np.sqrt(max(p0 * (1.0 - p0), 0.01) / n)
        scale = float(np.hypot(b["scale"], sampling))
        z = (late - p0) / scale
        out.append({"airport": ap, "hour": h, "n": n, "late_share": float(late),
                    "median": b["median"], "z": float(z),
                    "flag": bool(z >= Z_FLAG)})
    return out


def isolation_forest(day_rows: list[dict], base: dict, seed: int = 42):
    """A standard detector on the same features, as a check rather than a product."""
    from sklearn.ensemble import IsolationForest
    if not day_rows:
        return []
    X = np.array([[r["late_share"], r["median"], r["n"], r["hour"].hour]
                  for r in day_rows])
    clf = IsolationForest(contamination=0.05, random_state=seed).fit(X)
    pred = clf.predict(X)
    return [bool(p == -1) for p in pred]


def national_signal(rows: list[dict]) -> dict:
    """Share of airport-hours flagged, by hour -- the system-level alarm."""
    by_hour: dict = {}
    for r in rows:
        by_hour.setdefault(r["hour"], []).append(r)
    return {h: {"scored": len(v),
                "flagged": sum(x["flag"] for x in v),
                "share_flagged": sum(x["flag"] for x in v) / len(v),
                "mean_late": float(np.mean([x["late_share"] for x in v]))}
            for h, v in sorted(by_hour.items()) if len(v) >= 10}


def lead_time(signal: dict, alarm: float = 0.25) -> dict:
    """When does the alarm first fire, against when the day actually peaks?

    Murphy's whole claim is a head start. If the anomaly signal only rises once
    everything is already late, it is a description rather than a warning.
    """
    hours = sorted(signal)
    first = next((h for h in hours if signal[h]["share_flagged"] >= alarm), None)
    peak = max(hours, key=lambda h: signal[h]["mean_late"])
    return {"alarm_threshold": alarm,
            "first_alarm": f"{first:%H}:00Z" if first else None,
            "peak_lateness": f"{peak:%H}:00Z",
            "lead_hours": (peak - first).total_seconds() / 3600 if first else None}


if __name__ == "__main__":
    con = duckdb.connect()
    print("fitting 2023 baselines ...")
    base = fit_baseline(con)
    print(f"  {len(base):,} (airport, hour-of-day) slots with >=20 days of history")
    for label, day in (("storm", "2024-07-19"), ("calm", "2024-10-29")):
        rows = score_day(day, base, con)
        flagged = [r for r in rows if r["flag"]]
        iso = isolation_forest(rows, base)
        print(f"\n{label}  {day}   {len(rows):,} airport-hours scored")
        print(f"  robust-z flagged   {len(flagged):>5,}  "
              f"({100*len(flagged)/len(rows):5.1f}%)   median z "
              f"{np.median([r['z'] for r in rows]):+.2f}")
        print(f"  isolation forest   {sum(iso):>5,}  "
              f"({100*sum(iso)/len(iso):5.1f}%)")
        top = sorted(rows, key=lambda r: -r["z"])[:5]
        for r in top:
            print(f"     {r['airport']} {r['hour']:%H}:00Z  late {r['late_share']:.2f} "
                  f"vs usual {r['median']:.2f}   z {r['z']:+.1f}  n={r['n']}")
        sig = national_signal(rows)
        lt = lead_time(sig)
        print(f"  alarm (>={lt['alarm_threshold']:.0%} of airport-hours flagged) "
              f"first fires {lt['first_alarm']}, lateness peaks {lt['peak_lateness']}"
              + (f"  ->  {lt['lead_hours']:.0f} h lead" if lt["lead_hours"] else ""))
