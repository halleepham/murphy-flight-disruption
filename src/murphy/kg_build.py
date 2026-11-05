"""Extraction for the knowledge graph: Parquet rows in, node and edge records out.

Deliberately free of Neo4j calls. The counts this module produces are predicted in
docs/challenge-3/spec_challenge3.md, so a failure here surfaces against a test rather
than against a half-written database.

Two grains matter, and mixing them was the mistake the Phase 0 audit caught:

  * Flight, Weather and Aircraft come from the **loaded day(s)**. They are instances.
  * Service and the Airport/Airline statistics come from the **whole year**. They are
    aggregates, and at day grain a Service has roughly one flight, which tells you
    nothing.

Run:  PYTHONPATH=src .venv/bin/python -m murphy.kg_build
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
PARQUET = str(ROOT / "data" / "processed" / "flights" / "**" / "*.parquet")

# Decision D1: the US majors, by operating carrier -- "the plane I am sitting on."
# Regionals fly under mainline codes but are not in scope; see challenge3_log.md.
#
# Spirit (NK) was dropped on 12 October 2026: it ceased operations on 2 May 2026
# after two bankruptcies, the first major US carrier to fold in 25 years. Its 2024
# flights are still in the data, which is a concrete instance of the caveat the
# project plan already states -- historical performance carries over, the continued
# existence of a service does not.
MAJORS = ("AA", "DL", "UA", "WN", "AS", "B6", "F9", "G4", "HA")

# 19 July 2024 is the CrowdStrike outage: 58.2% of operated mainline flights arrived 15+
# minutes late and 16.9% of flights were cancelled, against a median day's 19.7% late in
# 2024. A real cascade that propagates through aircraft rotations
# is what the graph exists to show.
SEED_DAYS = ("2024-07-19",)

# Carrier names are not in the processed table. Ten rows from the BTS carrier lookup
# at https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ -- recorded as a source
# rather than invented.
CARRIER_NAMES = {
    "AA": "American Airlines",
    "DL": "Delta Air Lines",
    "UA": "United Air Lines",
    "WN": "Southwest Airlines",
    "AS": "Alaska Airlines",
    "B6": "JetBlue Airways",
    "F9": "Frontier Airlines",
    "G4": "Allegiant Air",
    "HA": "Hawaiian Airlines",
}

# A connection is not stored (decision S3) but the window still defines the sample
# itineraries built in U4.
MIN_CONNECTION_MIN = 45
MAX_CONNECTION_HOURS = 4

# Longest gap that still counts as the same rotation. A flying day spans at most about
# 19 hours, so 24 keeps every legitimate edge on a single loaded day. It exists for the
# case where SEED_DAYS holds days that are not adjacent: without it, an aircraft's last
# leg on one day chains to its first leg on the next loaded day, and the spatial
# continuity check does not catch it because the aircraft really was at that airport
# both times. Two non-adjacent days would produce 209 such edges, 2.3% of the total,
# wrong in a way no count would reveal.
MAX_ROTATION_GAP_HOURS = 24


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    return con


def _carriers_sql() -> str:
    return "(" + ", ".join(f"'{c}'" for c in MAJORS) + ")"


def _days_sql(days: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"DATE '{d}'" for d in days) + ")"


def _rows(con, sql: str) -> list[dict]:
    """Run a query and return plain dicts, which is what the driver wants to UNWIND.

    Timestamps are left as Python datetime objects rather than cast to strings. The
    driver maps those to Neo4j temporal types, and that is not cosmetic: a string
    timestamp accepts `+ duration({minutes: 45})` and silently returns the two
    concatenated -- '2024-07-19 09:15:00-05PT45M' -- so every connection-feasibility
    query would run, return plausible rows, and be wrong. Neo4j also cannot parse
    DuckDB's space-separated format; it wants ISO-8601 with a T and a colon in the
    offset. Passing the objects avoids both problems.
    """
    result = con.execute(sql)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


# --------------------------------------------------------------------------- nodes

def flights(con, days=SEED_DAYS) -> list[dict]:
    """Flight instances for the loaded day(s).

    Outcome columns carry an `outcome_` prefix because the standing rule is that
    arrival delay, departure delay and the cause codes are labels and never inputs.
    Naming puts the rule in front of anyone writing a query, instead of leaving it
    in a document they have not read.

    `causes_reported` exists because BTS populates cause columns only when arrival
    delay is 15 minutes or more. Without the flag, a null reads as "weather was not a
    factor" when it actually means "BTS does not attribute delays this small."
    """
    return _rows(con, f"""
        SELECT
            flight_id,
            carrier,
            flight_number,
            tail_number,
            flight_date,
            origin, dest,
            dep_utc                                       AS sched_dep_utc,
            arr_utc                                       AS sched_arr_utc,
            sched_dep_local, sched_arr_local,
            sched_duration_min, sched_dep_hour, distance_mi, day_of_week,
            arr_delay_min                                 AS outcome_arr_delay_min,
            dep_delay_min                                 AS outcome_dep_delay_min,
            cancelled                                     AS outcome_cancelled,
            cancellation_code                             AS outcome_cancellation_code,
            diverted                                      AS outcome_diverted,
            delay_carrier_min                             AS outcome_delay_carrier_min,
            delay_weather_min                             AS outcome_delay_weather_min,
            delay_nas_min                                 AS outcome_delay_nas_min,
            delay_security_min                            AS outcome_delay_security_min,
            delay_late_aircraft_min                       AS outcome_delay_late_aircraft_min,
            (delay_carrier_min IS NOT NULL)               AS causes_reported,
            origin || '-' || dest || '-' || carrier || '-'
                   || CAST(sched_dep_hour AS VARCHAR)     AS service_id,
            -- A display caption. Neo4j's viewers default to a property called `name`
            -- and offer nothing else when none exists, so without this every node in
            -- a screenshot is captioned with its element id. Display only: never read
            -- by a query, and every value in it is reachable through an edge.
            carrier || CAST(flight_number AS VARCHAR) || ' '
                    || origin || '-' || dest           AS name
        FROM read_parquet('{PARQUET}', hive_partitioning=true)
        WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
    """)


def services(con, days=SEED_DAYS) -> list[dict]:
    """Services the loaded day touches, with distributions computed over the whole year.

    This is where the precomputed numbers live. An LLM narrating a retrieved subgraph
    cannot derive a quantile from it and will invent one if it is missing, so anything
    the narration might cite has to be a stored property.

    The cancellation rate is computed over every flight including cancelled ones, while
    the delay quantiles are computed only over flights that actually operated -- a
    cancelled flight never arrived, so it says nothing about lateness.
    """
    return _rows(con, f"""
        WITH touched AS (
            SELECT DISTINCT origin, dest, carrier, sched_dep_hour
            FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
        )
        SELECT
            f.origin || '-' || f.dest || '-' || f.carrier || '-'
                     || CAST(f.sched_dep_hour AS VARCHAR)            AS service_id,
            f.origin, f.dest, f.carrier, f.sched_dep_hour,
            count(*) FILTER (WHERE f.cancelled = 0
                               AND f.arr_delay_min IS NOT NULL)      AS n_flights,
            CAST(quantile_cont(f.arr_delay_min, 0.10)
                 FILTER (WHERE f.cancelled = 0) AS INT)              AS p10_arr_delay,
            CAST(quantile_cont(f.arr_delay_min, 0.50)
                 FILTER (WHERE f.cancelled = 0) AS INT)              AS p50_arr_delay,
            CAST(quantile_cont(f.arr_delay_min, 0.90)
                 FILTER (WHERE f.cancelled = 0) AS INT)              AS p90_arr_delay,
            round(100.0 * avg(CASE WHEN f.cancelled = 0
                                   THEN (f.arr_delay_min >= 15)::INT END), 2) AS pct_late15,
            round(100.0 * avg(f.cancelled), 2)                       AS cancellation_rate,
            CAST(median(f.sched_dep_minutes) AS INT)                 AS median_dep_minutes,
            CAST(median(f.sched_duration_min) AS INT)                AS median_duration_min,
            any_value(f.carrier) || ' ' || any_value(f.origin) || '-' || any_value(f.dest)
                || ' ' || CAST(any_value(f.sched_dep_hour) AS VARCHAR) || 'h' AS name
        FROM read_parquet('{PARQUET}', hive_partitioning=true) f
        JOIN touched t
          ON f.origin = t.origin AND f.dest = t.dest
         AND f.carrier = t.carrier AND f.sched_dep_hour = t.sched_dep_hour
        WHERE f.carrier IN {_carriers_sql()}
        GROUP BY 1, 2, 3, 4, 5
    """)


def aircraft(con, days=SEED_DAYS) -> list[dict]:
    """One node per tail number flying on the loaded day(s).

    Legs and rotation depth are deliberately not stored -- they are what the PRECEDES
    traversal is for, and storing them would mean maintaining a derived count.
    """
    return _rows(con, f"""
        SELECT tail_number,
               tail_number        AS name,
               arg_max(carrier, n) AS operator
        FROM (
            SELECT tail_number, carrier, count(*) AS n
            FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
              AND tail_number IS NOT NULL
            GROUP BY 1, 2
        )
        GROUP BY tail_number
    """)


def airports(con, days=SEED_DAYS) -> list[dict]:
    """Airports as origin OR destination, with year-level reliability.

    The union matters: STC appears exactly once in 2024 as a destination and never as
    an origin, almost certainly a diversion. Building from origins alone would leave a
    dangling ARRIVES_AT edge.
    """
    return _rows(con, f"""
        WITH slice AS (
            SELECT * FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
        ),
        seen AS (
            SELECT origin AS iata, origin_tz AS tz FROM slice
            UNION
            SELECT dest, dest_tz FROM slice
        ),
        yearly AS (
            SELECT origin AS iata,
                   count(*)                                       AS n_departures,
                   CAST(median(dep_delay_min) AS INT)             AS median_dep_delay,
                   round(100.0 * avg((arr_delay_min >= 15)::INT), 2) AS pct_late15
            FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND cancelled = 0
            GROUP BY 1
        )
        SELECT s.iata, s.iata AS name, any_value(s.tz) AS timezone,
               any_value(y.n_departures) AS n_departures,
               any_value(y.median_dep_delay) AS median_dep_delay,
               any_value(y.pct_late15) AS pct_late15
        FROM seen s LEFT JOIN yearly y USING (iata)
        GROUP BY s.iata
    """)


def airlines(con) -> list[dict]:
    """The ten majors, with year-level operating statistics.

    ATCR complaint and baggage rates belong here too (provenance tier: Reported) but
    are not yet ingested -- the source is a monthly PDF. Added in a later unit.
    """
    rows = _rows(con, f"""
        SELECT carrier,
               count(*)                                          AS n_flights,
               CAST(median(arr_delay_min) AS INT)                 AS median_arr_delay,
               round(100.0 * avg((arr_delay_min >= 15)::INT), 2)  AS pct_late15
        FROM read_parquet('{PARQUET}', hive_partitioning=true)
        WHERE carrier IN {_carriers_sql()} AND cancelled = 0
        GROUP BY 1
    """)
    cancels = {
        r["carrier"]: r["cancellation_rate"]
        for r in _rows(con, f"""
            SELECT carrier, round(100.0 * avg(cancelled), 2) AS cancellation_rate
            FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} GROUP BY 1
        """)
    }
    for r in rows:
        r["name"] = CARRIER_NAMES[r["carrier"]]
        r["cancellation_rate"] = cancels[r["carrier"]]
    return rows


def weather(con, days=SEED_DAYS) -> list[dict]:
    """One node per airport-hour, built from both ends of every flight.

    Safe to merge the two ends: for overlapping airport-hours, the origin-side and
    destination-side observations agree on 100% of rows, so there is one observation
    per airport-hour rather than two competing ones.
    """
    return _rows(con, f"""
        WITH slice AS (
            SELECT * FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
        ),
        both_ends AS (
            SELECT origin AS iata, date_trunc('hour', dep_utc) AS hour_utc,
                   origin_temp_c AS temp_c, origin_wind_ms AS wind_ms,
                   origin_precip_mm AS precip_mm, origin_visibility_m AS visibility_m,
                   origin_ceiling_m AS ceiling_m, origin_fog AS fog,
                   origin_thunderstorm AS thunderstorm
            FROM slice WHERE origin_visibility_m IS NOT NULL
            UNION ALL
            SELECT dest, date_trunc('hour', arr_utc),
                   dest_temp_c, dest_wind_ms, dest_precip_mm, dest_visibility_m,
                   dest_ceiling_m, dest_fog, dest_thunderstorm
            FROM slice WHERE dest_visibility_m IS NOT NULL
        )
        SELECT iata || '-' || strftime(hour_utc, '%Y-%m-%dT%H') AS weather_id,
               iata || ' ' || strftime(hour_utc, '%d %b %Hh')    AS name,
               iata, hour_utc,
               max(temp_c) AS temp_c, max(wind_ms) AS wind_ms,
               max(precip_mm) AS precip_mm, max(visibility_m) AS visibility_m,
               max(ceiling_m) AS ceiling_m, max(fog) AS fog,
               max(thunderstorm) AS thunderstorm
        FROM both_ends
        GROUP BY iata, hour_utc
    """)


# --------------------------------------------------------------- relationship edges

def precedes(con, days=SEED_DAYS) -> list[dict]:
    """Consecutive legs of the same aircraft, where the chain closes spatially.

    98.06% of consecutive pairs across the full table satisfy "leg N's destination is
    leg N+1's origin." The remainder are maintenance ferry flights that BTS does not
    collect, tail reassignments, and diversions in between. Requiring continuity keeps
    the rotation backbone honest: an edge here means the same airframe really did
    continue, not merely that two flights share a tail number.
    """
    return _rows(con, f"""
        WITH chained AS (
            SELECT flight_id, dest, dep_utc,
                   lead(flight_id) OVER w AS next_id,
                   lead(origin)    OVER w AS next_origin,
                   lead(dep_utc)   OVER w AS next_dep
            FROM read_parquet('{PARQUET}', hive_partitioning=true)
            WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
              AND tail_number IS NOT NULL AND cancelled = 0 AND diverted = 0
            -- flight_id breaks ties. Three tails on the seed day have two flights
            -- stamped at the same instant (N3157J is BOS->JFK and JFK->BOS at once),
            -- which is a scheduling artefact rather than reality. Ordering by dep_utc
            -- alone leaves lead() free to pick either, so two loads could produce
            -- different edges and the graph would stop being reproducible.
            WINDOW w AS (PARTITION BY tail_number ORDER BY dep_utc, flight_id)
        )
        SELECT flight_id AS from_id, next_id AS to_id,
               CAST(datediff('minute', dep_utc, next_dep) AS INT) AS gap_min
        FROM chained
        WHERE next_id IS NOT NULL
          AND next_origin = dest
          AND datediff('hour', dep_utc, next_dep) <= {MAX_ROTATION_GAP_HOURS}
    """)


def service_endpoints(con, days=SEED_DAYS) -> list[dict]:
    """Origin and destination for each Service, so routing is a traversal.

    Services reach airports through the same DEPARTS_FROM / ARRIVES_AT relationships
    Flights use. That is what lets route planning walk
    Airport <- Service -> Airport <- Service -> Airport without a CONNECTS_TO edge
    between services, which would have been combinatorial.
    """
    return _rows(con, f"""
        SELECT DISTINCT
            origin || '-' || dest || '-' || carrier || '-'
                   || CAST(sched_dep_hour AS VARCHAR) AS service_id,
            origin, dest, carrier
        FROM read_parquet('{PARQUET}', hive_partitioning=true)
        WHERE carrier IN {_carriers_sql()} AND flight_date IN {_days_sql(days)}
    """)


# ---------------------------------------------------------------------------- main

def summary(con, days=SEED_DAYS) -> dict:
    """Every batch, with its size. Used by the loader and by the test."""
    return {
        "Flight": flights(con, days),
        "Service": services(con, days),
        "Aircraft": aircraft(con, days),
        "Airport": airports(con, days),
        "Airline": airlines(con),
        "Weather": weather(con, days),
        "PRECEDES": precedes(con, days),
        "ServiceEndpoints": service_endpoints(con, days),
    }


def main():
    con = connect()
    batches = summary(con)
    nodes = sum(len(v) for k, v in batches.items()
                if k not in ("PRECEDES", "ServiceEndpoints"))
    print(f"seed day(s): {', '.join(SEED_DAYS)}   carriers: {len(MAJORS)}\n")
    for name, rows in batches.items():
        print(f"  {name:<18} {len(rows):>8,}")
    print(f"\n  {'NODES (excl. policy/session)':<18} {nodes:>8,}")

    # Relationship arithmetic, so the budget is visible rather than asserted.
    f, s, w = len(batches["Flight"]), len(batches["Service"]), len(batches["Weather"])
    # Cancelled flights carry no tail number -- BTS assigns one only when an aircraft
    # actually operated -- so FLIES is short of the flight count by exactly those.
    flies = sum(1 for r in batches["Flight"] if r["tail_number"])
    rels = (flies
            + len(batches["PRECEDES"])
            + f                     # INSTANCE_OF
            + f + s                 # DEPARTS_FROM
            + f + s                 # ARRIVES_AT
            + s                     # OPERATED_BY
            + w)                    # OBSERVED_AT
    print(f"  {'RELATIONSHIPS':<18} {rels:>8,}")


if __name__ == "__main__":
    main()
