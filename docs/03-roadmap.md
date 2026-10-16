# 03 — Roadmap

Each milestone is independently demonstrable. Milestone 1 is done.

## M1 — Ingest slice ✅

**One release, raw file to canonical Parquet, reproducibly.**

- Delimiter and header-row detection past a metadata preamble
- Header aliasing across a dozen names per quantity
- Unit recovery from the header text (`Voltage(mV)`, `Time_ms`, `chg_cap_mAh`)
- Sign-convention inference with the evidence printed
- ISO/epoch timestamps → per-cell elapsed seconds
- Independent Coulomb-count cross-check against the reported capacity column
- Checksum-verified, atomic, resumable fetch
- Atomic resumable job state
- Licence gate that refuses to publish non-redistributable rows

**Evidence:** 70 tests pass; a Parquet artifact written by the pipeline was read
back with duckdb alone and shown to carry `DOUBLE` measurements and `BIGINT`
indices with correctly signed currents.

## M2 — The corpus

**All 23 field releases in one schema, with a quality report per release.**

- Import the collection's metadata registry into `registry.py`
- `.mat` and `.h5` readers (NASA ships `.mat`; several field releases are HDF5)
- Per-release dataset card: licence, units found, sign convention, coverage,
  fault labels, and every unmapped column with the decision taken
- Partitioned Parquet on HF Hub: `source_id=.../cell_id=...`
- **Definition of done:** a single query joins all 23 releases without a
  per-release special case leaking into the analysis layer

**Risks:** formats will not all be CSV-like; some releases may be
non-redistributable and must stay local.

## M3 — The ingestion agent

**Automation, not a script with a cron line.**

- An agent that proposes a column map for an unseen release, then verifies the
  proposal against the independent Coulomb-count check before accepting it
- Escalation: when the check fails, the agent reads the release's own paper
  (RAG) to find the documented convention rather than guessing
- Actions workflow: schedule, matrix over releases, artifact upload, retry
- **Definition of done:** an unseen release is ingested without a human editing
  the alias map, and the agent's reasoning is in the log

## M4 — Diagnostics

**Health estimation on field data, honestly.**

- Feature extraction from partial, interrupted, real-duty cycles
- Capacity estimation where no full discharge ever occurred — the field problem
- Ambient-temperature regression against SMHI/ERA5 site series, because without
  it "degradation" is largely a seasonal artefact
- Fault detection against the **784 labelled faults**
- **Definition of done:** a held-out fault-detection result with confidence
  intervals, and an explicit statement of what the model cannot detect

## M5 — The RL layer

**When to spend a capacity test.** The reason the RL requirement is satisfied
rather than name-dropped.

- State: estimated health, uncertainty, time since last test, recent duty profile,
  fault-flag history
- Action: run a capacity test now / defer by one interval / flag for inspection
- Reward: detection accuracy **minus** the cost of the test — a capacity test
  costs real cycles and real downtime, which is what makes this a trade-off
  rather than a classification problem
- Comparison against the obvious baselines: fixed-interval testing, and
  threshold-on-uncertainty
- **Definition of done:** a learned policy beats fixed-interval scheduling on
  cost-adjusted detection at equal test budget, on held-out systems

This is a partially observed sequential decision problem, which is the
interesting part: the model is deciding what to learn, not what a label is.

## M6 — Search and briefing

**Orchestration plus RAG producing something a human acts on.**

- Natural-language query over the corpus, grounded in dataset cards and metrics
- A briefing that answers the actual question: *which units should be inspected
  first, and what is the evidence*
- HF Space (CPU, free) as the interface
- **Definition of done:** the briefing cites the release, the cells and the
  quantity behind every claim, and refuses to answer when the corpus cannot
  support an answer

## Deliberately out of scope

- Physical hardware. No sensors, no capital.
- Beating the state of the art on lab SOH benchmarks — crowded, and not the
  question this project asks.
- A hosted multi-tenant product. The compute budget is 30 GPU-hours a week.
