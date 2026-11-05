# Does a trained model beat looking up similar flights?

The question Challenge 2 was built to answer. **Yes, by about 11%.**

Reproduce with:

```
.venv/bin/python src/murphy/model.py
```

## The two methods

**L1 — lookup.** The empirical 10th, 50th and 90th percentiles of arrival delay
across comparable flights: same route, carrier and departure-hour band, with the
same fallback the application uses when a match is thin. This is what the app
currently shows, and it is what any careful person would build first.

**L2 — learned.** Gradient-boosted quantile regression over the same information
expressed as features, fitting all three quantiles directly against pinball loss.

Twelve features, chosen to stay explainable: carrier, origin, destination, month,
day of week, departure hour, scheduled duration, distance, and temperature, wind,
precipitation and visibility at the origin.

**Nothing that would be unknown before departure is used.** No departure delay,
no taxi time, no actual times, no delay-cause codes. Those describe what
happened; a forecaster does not have them.

## Protocol

- **Train on 2023, test on 2024.** A full year apart. Neither method sees any
  part of the year it is scored on.
- **Identical test rows, identical metrics** — 6,860,395 flights that operated, from
  all 15 carriers in the table, including Spirit and regional carriers.
- Cancelled flights are excluded from both: they have no arrival delay, so they
  cannot teach or test a model about lateness.
- Training samples 2,000,000 of 2023's 6.6 million usable rows. Training on all
  of them at depth 8 ran for over an hour without finishing, which makes the
  model impossible to iterate on; the constraint here is signal, not volume.

## Results

| | pinball (mean) | p10 | p50 | p90 | 80% coverage | median abs. error | interval width |
|---|---|---|---|---|---|---|---|
| **L1 lookup** | 9.542 | 3.952 | 12.475 | 12.198 | 70.9% | 13.0 min | 59.0 min |
| **L2 model** | **8.476** | **3.383** | **11.543** | **10.502** | **77.0%** | **10.9 min** | 59.2 min |

**L2 is 11.2% better on pinball loss**, and better at every individual quantile.

**The interval width is the important column.** L2 is not winning by hedging —
its intervals are the same width as L1's (59.2 against 59.0 minutes) while
covering more of the truth and sitting closer to it. A model that improved
coverage by widening its intervals would have learned nothing; this one has not
done that.

**Neither is calibrated, and L2 is much closer.** An 80% interval should contain
the outcome 80% of the time. L1 manages 70.9%, L2 manages 77.0%. Both
under-cover, so both are overconfident — but L1 is overconfident by nine points
and L2 by three. Closing that gap properly is conformal prediction, which is
Challenge 3 work, not something to claim here.

## What the model leans on

By gain: **carrier, departure hour, origin, month, destination, distance,
scheduled duration, origin visibility.**

Two things worth noting. Departure hour ranking second matches what the lookup
baseline already encoded by matching on it. And **visibility appearing at all
justifies the harder weather source** — it does not exist in reanalysis data, and
the model uses it.

## Honest caveats

- **Diminishing returns.** Thirty boosting rounds scored 8.514; two hundred and
  fifty scored 8.476. Almost all the gain arrives early, which suggests the
  feature set, not the model capacity, is the limit.
- **An 11% improvement in pinball loss is not a transformed product.** The median
  absolute error moves from 13.0 to 10.9 minutes. Real, and worth having, but
  L1 remains a respectable baseline — which is itself the finding. The lookup
  table is hard to beat because retrieval over comparable flights is genuinely
  informative.
- **This measures forecast quality, not decision quality.** Whether the better
  forecast produces better decisions is a separate question, and the route
  evaluation in `EVALUATION.md` is the first attempt at it.
- **One year of training, one of testing.** No cross-validation across years,
  and 2023 and 2024 were both relatively ordinary years for US aviation.
