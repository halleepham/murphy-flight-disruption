# Measured results

Committed so the numbers are readable without running anything. These files are
regenerated, never hand-edited.

| File | Written by | Holds |
|---|---|---|
| `gnn_results.json` | `notebooks/04_gnn_ablation.py` | the five-rung ablation at both query slacks, five seeds each |
| `gnn_figure_data.json` | `notebooks/05_gnn_figures.py` | per-seed AUCs with and without the inbound-delay feature |
| `communities.json` | `notebooks/09_communities.py` | hourly communities on the three test days, with routing results |
| `anomaly.json` | `notebooks/10_anomaly.py` | the anomaly detector on the CrowdStrike and calm days |
| `app_outputs.json` | `notebooks/11_app_outputs.py` | the data behind the two Stage 2 generated outputs |
| `c2_validation.json` | `notebooks/12_validate_c2.py` | the test of rotation-coupled communities |
| `retrieval.json`, `community_summaries.json` | `notebooks/13_retrieval_summaries.py` | community retrieval against plain rows, and the summaries |
| `fixed_vs_dynamic.json` | `notebooks/14_fixed_vs_dynamic.py` | fixed regions against hourly communities, the top-pick results quoted in the README |
| `carrier_communities.json` | `notebooks/15_carrier_communities.py` | the test of carrier communities |
| `alarm_2024.json` | `notebooks/16_alarm_year.py` | how many days of 2024 the alarm fires. A count, not an accuracy measure |
| `codesign/` | `murphy.codesign_export` | everything the web app displays |

The GNN files need a loaded Neo4j instance and `requirements-gnn.txt`; the ablation trains
20 models and takes a few minutes. The others read the Parquet tables, and the app export
needs a Groq key for captions unless run with `--no-captions`.

`split_boundary` in `gnn_results.json` is the temporal train/test cut — every test
flight was scheduled to depart after it. The GNN results cover Southwest flights on
19 July 2024 only.
