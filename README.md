# Murphy

**Will my trip hold — and should I trust the answer?**

Murphy is a passenger-facing app that forecasts flight disruption risk and says how
much to trust that forecast. It checks a traveller's booked flights against what the
whole flight network is doing, recommends whether to switch, and warns when the day
is too unusual for its own history to apply.

The research claim behind it: *a forecasting system that knows when to distrust itself
makes better decisions than one that is merely accurate.*

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

**The LLM never produces a number.** It parses text and explains results. Every value
on screen comes from data, and every claim names its source: observed flights, derived
statistics, quoted policy documents, or the traveller's own choices.

## Key results

| Question | Finding |
|---|---|
| Does a trained model beat looking up similar flights? | Yes. XGBoost pinball loss 8.476 against 9.542 for retrieval |
| Are the forecast ranges trustworthy? | Not yet. 77% of outcomes fall inside an 80% interval |
| Does risk-aware routing help? | It halved missed connections, from 8.1% to 4.2% |
| Does a graph neural network add anything? | No. GraphSAGE matches a plain MLP (0.791 against 0.792 AUC), because outcomes are never used as node features |
| Do airport communities improve decisions? | Yes, on bad days. The top recommendation held 65% → 80% of the time on a winter-storm day and 75% → 80% during the CrowdStrike outage, with nothing to gain on a calm day |
| Can the system tell when not to trust itself? | The anomaly alarm fires 18 hours before lateness peaks on the CrowdStrike day, and never on the calm day |

## Data

- **BTS On-Time Performance** joined with **NOAA Integrated Surface Database** weather:
  13.7 million flights, 2023–2024
- **Scope:** nine US major carriers by operating carrier — `AA DL UA WN AS B6 F9 G4 HA`
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

- **Three test days.** The community and anomaly results do not yet generalise across
  seasons
- **Historical replay, not live.** BTS publishes months after the fact, so the app
  checks sample trips from real past days rather than future flights
- **Forecast ranges are not calibrated**, and the app says so on screen
- **Regional carriers are out of scope**, which removes 86 airports and 1,152 routes
- **One global 45-minute connection rule**; real minimum connection times vary by
  airport
- **No fare data**, so cost is never an objective
