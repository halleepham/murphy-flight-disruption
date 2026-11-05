# Murphy

**Will my trip hold — and should I trust the answer?**

Murphy is a passenger-facing app that forecasts flight disruption risk and says how
much to trust that forecast. It checks a traveller's booked flights against what the
whole flight network is doing, recommends whether to switch, and warns when the day
is too unusual for its own history to apply.

The research claim behind it: *a forecasting system that knows when to distrust itself
makes better decisions than one that is merely accurate.* The evidence so far is
promising, but the claim has not been tested directly. See [Key results](#key-results).

Course project for CS 5542, Big Data Analytics and Applications, Fall 2026, by Hallee
Pham.

---

## Who it is for

Travellers with something to lose: a connection to make, a deadline to meet, or just a
flight to land on time. Airlines already hold crew, gate and load data; passengers
only have public data. Murphy is built from public data alone.

## How it works

| Piece | What it does |
|---|---|
| **Booking parser** | An LLM turns a pasted confirmation into a validated itinerary. Missing fields stay empty for the traveller to fill in; nothing is guessed |
| **Comparable-flight retrieval** | SQL over Parquet finds historical flights like this one and returns an arrival-delay range, with the rows behind it |
| **Forecast model** | XGBoost quantile regression predicts the range of arrival delay |
| **Knowledge graph + GraphRAG** | Neo4j links flights, aircraft rotations, airports, weather and airline policies. Parameterised Cypher retrieves evidence that an LLM explains |
| **Community detection** | Leiden groups nearby airports that tend to fail together, so an alternative that shares the origin's problem is ranked down |
| **Anomaly detection** | Flags airports running late in a way unusual for that airport at that hour, giving the app a "today is not a normal day" signal |
| **Web app** | A FastAPI server with a hand-built front end: trip input, a verdict, an interactive route map, ranked options, an arrival range against the traveller's deadline, the reasons behind each leg, cited passenger rights, and an animated replay of the day with AI-written captions |

**What the web app uses today:** retrieval, communities, anomaly detection and the
quoted policies. The booking parser, the forecast model, the knowledge graph, GraphRAG
and the GNN were built and tested separately and are not connected to it yet.

**The LLM never computes a number.** It parses text and explains results, and a checker
rejects any caption containing a number it was not given. Every value on screen comes
from data, and every claim names its source: observed flights, derived
statistics, quoted policy documents, or the traveller's own choices.

## Key results

| Question | Finding |
|---|---|
| Does a trained model beat looking up similar flights? | Yes. XGBoost pinball loss 8.476 against 9.542 for retrieval, lower is better |
| Are the forecast ranges trustworthy? | Not yet. 77% of outcomes fall inside an 80% interval |
| Does risk-aware routing help? | Yes, at a cost. Missed connections fell from 8.1% to 4.2%, for about 10 more minutes of median travel. Total time ties once each miss is charged a stated 240 minutes. Tested on 10 routes in October 2024 |
| Does a graph neural network add anything? | No. GraphSAGE matches a plain MLP (0.791 against 0.792 AUC), because outcomes are never used as node features. Tested on Southwest flights from one day, 19 July 2024 |
| Do airport communities improve decisions? | On the winter-storm day, yes: the top recommendation held 65% → 80% of the time (14 better, 2 worse). The CrowdStrike gain, 75% → 80%, could be noise (13 better, 8 worse). A calm day had little to gain, 99% → 100% |
| Can the system tell when not to trust itself? | Partly. The anomaly alarm fired on both bad days and not on the calm day, though that day came close (23.7% of airports against a 25% threshold). It fires on 34 of 366 days in 2024, and its accuracy has not been measured. On CrowdStrike it fired in the first hour of data, about 6 hours after the outage began, when airports already averaged 53% late |
| Does knowing when to distrust itself improve decisions? | Not tested yet. The alarm changes the app's trust message, not its recommendation, so nothing compares decisions with and without it |

## Data

- **BTS On-Time Performance** joined with **NOAA Integrated Surface Database** weather:
  13.7 million flights, 2023–2024
- **Scope:** the knowledge graph, communities and the app's route options use nine US
  major carriers by operating carrier — `AA DL UA WN AS B6 F9 G4 HA`. The GNN uses
  Southwest only. The forecast comparison, routing evaluation and anomaly detection use
  all 15 carriers in the table, including Spirit and regional carriers
- **Policy sources:** the DOT Airline Customer Service Dashboard and two contracts of
  carriage
- **Test days:** 15 January 2024 (winter storm), 19 July 2024 (CrowdStrike outage) and
  29 October 2024 (an ordinary day)

The 2024 flight table is committed, so a clone runs without a download. The 2023–24
table is rebuilt with `fetch_bts.py`, `fetch_isd.py` and `build_parquet_bts.py`.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add a Groq key, and Neo4j Aura credentials for the graph
```

**The web app** reads precomputed results committed in `results/codesign/`, so it runs
with no database or API key:

```bash
.venv/bin/python -m uvicorn app.web.server:app --port 8000
# open http://localhost:8000
```

**Rebuilding** the app's results needs the 2023–24 data:

```bash
PYTHONPATH=src .venv/bin/python -m murphy.codesign_export
```

**The knowledge graph** loads into a free Neo4j Aura instance in about a minute:

```bash
PYTHONPATH=src .venv/bin/python -m murphy.kg_load
PYTHONPATH=src .venv/bin/python -m murphy.kg_congestion
```

**Tests:**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

Graph tests skip without a reachable Neo4j instance.

## Repository layout

```
app/web/          the web app: FastAPI server and static front end
src/murphy/       parser, retrieval, model, route planner, knowledge graph,
                  GraphRAG, GNN, communities, anomaly detection, app export
notebooks/        audits and experiments, one per stage
results/          measured results, committed so they read without a re-run
data/processed/   Parquet flight tables
screenshots/      captures of the graph and the app
tests/            pytest suite
```

## Limitations

- **The main claim is untested.** The alarm does not yet change the app's advice, and
  its accuracy has not been measured
- **Three test days.** The community results do not yet generalise across seasons
- **No live data.** BTS publishes months after the fact, so the app shows five
  precomputed trips from real past days and cannot check your own flight
- **No future schedules.** Route options come from flights that operated in the past,
  so a flight may no longer exist or have seats
- **Forecast ranges are not calibrated**, and the app says so on screen
- **Regional carriers are out of scope for the app**, which removes 86 airports and
  1,152 routes
- **Captions can still mislead.** Their numbers are checked, but their meaning is not:
  the calm-day caption says "no abnormal airport delays" when up to 24% were flagged
- **One global 45-minute connection rule**; real minimum connection times vary by
  airport
- **No fare data**, so cost is never an objective
