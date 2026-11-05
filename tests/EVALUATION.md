# Evaluation — does risk-aware ranking beat ranking by scheduled time?

The comparison the instructor feedback asks for, on real outcomes.
Reproduce with:

```
.venv/bin/python src/murphy/evaluate.py
```

## Method

**Baseline** ranks itineraries by scheduled duration — what a booking site shows.
**Murphy** ranks by what comparable flights historically did door to door, penalised
by the share of inbound flights that arrived too late to make the transfer.

Both choose an itinerary for a real origin, destination, date and target departure
time. We then look up what actually happened to each one's choice.

- **Ranking sees 2023 only. Scoring uses October 2024.** A full year apart, so
  nothing the ranker saw can contain the outcome it is judged on.
- **Ten routes**, six with roughly one nonstop a day and dozens of connecting
  options, four well served — the latter check that the method does not connect
  needlessly.
- **Three target departure times a day** (07:00, 12:00, 17:00), candidates within
  three hours. Comparing a 06:00 departure against a 15:00 one is not comparing
  substitutes.
- Cancelled flights are excluded from both the history and the candidate set: a
  flight that did not operate is not an itinerary anyone could have taken.

```
RESULTS — 930 planning decisions, 10 routes, month 10 of 2024
==================================================================

The two methods chose the same itinerary 474 times (51%).
They differed on 456 decisions — those are where the comparison lives.

Completed trips only — missed connections excluded

                median      mean       p90        missed
--------------------------------------------------------
Scheduled         315m      319m      403m       75 (8.1%)
Murphy            325m      330m      419m       39 (4.2%)

With missed connections charged 240 minutes
(a stated assumption, not a measurement — we have no data on what
rebooking actually costs)

                median      mean       p90
------------------------------------------
Scheduled         324m      341m      432m
Murphy            329m      340m      436m

Door-to-door minutes, from scheduled departure to actual arrival.

==================================================================
PAIRED — same route, same day, only where they disagreed
==================================================================

404 paired decisions where the two methods chose differently.

  Murphy arrived earlier    108  (27%)
  Murphy arrived later      293  (73%)
  Same arrival                3  (1%)

  Mean difference         +14.4 min (negative favours Murphy)
  Median difference       +12.0 min
  Worst case for Murphy    +248 min
  Best case for Murphy     -210 min

  Connections missed by the scheduled-time pick but not Murphy: 43
  Connections missed by Murphy but not the scheduled-time pick: 7

  Decisions where the two disagreed about whether to connect at all: 0 of 456
```

## What this shows

**Risk-aware ranking halves the missed-connection rate and costs about ten
minutes.** It is not a win, and saying so is more useful than claiming one.

| | Scheduled-time | Murphy | |
|---|---|---|---|
| Missed connections | 75 (8.1%) | **39 (4.2%)** | **−48%** |
| Median door-to-door, completed trips | 315 min | 325 min | +10 min |
| Mean, failures charged 240 min | 341 min | **340 min** | −1 min |

On 930 planning decisions the two agreed 51% of the time — whenever a nonstop
was available in the departure window, both took it. Murphy does not connect
needlessly.

On the 456 decisions where they differed, Murphy arrived **later** 73% of the
time, by a median of 12 minutes. In exchange it avoided 43 connection failures
the scheduled-time ranking walked into, while causing 7 of its own.

**Once missed connections are charged, the two methods are a dead heat on total
journey time** — 340 minutes against 341. That is the honest headline. Risk-aware
ranking converts a small, certain time cost into a large reduction in the chance
of a bad day, and at a 240-minute penalty those very nearly cancel out.

Which means the answer to *"does the improved approach provide useful value?"*
depends on something the data cannot settle: how much a traveller hates missing a
connection. Someone with a deadline should take the trade. Someone who simply
wants to be home sooner on an average day should not. **That is a preference, not
a fact** — which is why the application exposes the objectives and lets the
traveller rank by them, rather than choosing on their behalf.

## Limitations of this evaluation

- **A missed connection's real cost is unknown.** The penalised view charges a
  flat 240 minutes. Since the two methods finish within a minute of each other
  under that assumption, the conclusion is sensitive to it: a higher penalty
  favours Murphy, a lower one favours the baseline.
- **Connections are constructed, not published.** A one-stop itinerary pairs a
  real arrival with a real departure under a 45-minute rule, not a fare an
  airline sells.
- **Cancellation is excluded rather than modelled.** The data now contains
  cancelled flights, but the planner does not yet use historical cancellation
  rates when ranking. That is the obvious next improvement.
- **All 15 carriers, and connections may change carrier.** This evaluation does not
  apply the nine-carrier scope or the app's one-carrier connection rule.
- **One month, ten routes.** Enough to see a consistent effect on connection
  reliability; not enough for a general claim.
- **No fare data**, so cost never enters the ranking.
