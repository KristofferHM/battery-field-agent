# 00 — Overview

## The problem

Public battery data exists in volume and not in a usable form. The largest single
collection — 23 releases from 17 research groups, **226 GB, 131,096 cells, 3,025
systems, 44.4 MWh, 2,918 unit-years, 784 labelled faults** — is published as
heterogeneous files whose authors found it necessary to ship a separate metadata
registry *precisely because* every release carries its own format, units and
labels. That registry is the start of a pipeline, not the pipeline.

Meanwhile the value question in the battery industry is not "how does this cell
behave in a lab cycler" but **"which unit in the field is going to fail, and when
do I spend a maintenance visit to find out"**. Answering that needs field data in
one shape, joined to a decision policy.

## What this project is

An ingestion, normalisation, diagnostics and agent layer over that corpus, built
to run on **free-tier cloud compute only**.

Three claims, in order of how much they matter:

1. **Schema reconciliation is the hard part, and it is tractable.** Header
   aliasing, unit recovery from the header text, sign-convention inference and
   Coulomb-counting cross-checks turn 23 incompatible exports into one schema —
   deterministically, with the decisions made visible rather than guessed.
2. **The compute constraint is engineerable, not fatal.** A daily pipeline can
   run on GitHub Actions (free for public repos) for orchestration, Kaggle's
   30 GPU-h/week for training, and Hugging Face Hub for durable state, with no
   server and no spend.
3. **Diagnostic scheduling is a genuine sequential decision problem.** Deciding
   *when* to run an expensive capacity test is a cost-versus-information
   trade-off over a partially observed state — a natural reinforcement-learning
   formulation, not a bolted-on flourish.

## Who this is for

Someone who needs field battery data in one shape and a defensible answer to
"which units do I test first". The consumer of the output is a maintenance
planner, not a researcher.

## What this is not

- Not a state-of-health leaderboard entry. There is a large literature on lab
  SOH regression; re-running it on NASA and Oxford data adds nothing.
- Not a self-driving-lab simulator. That needs hardware.
- Not a general "AI for materials" framework. The scope is field battery data and
  the maintenance decision it supports.

## Shape of the repo

| Path | Role |
|---|---|
| `src/battery_field_agent/normalize.py` | the reconciliation core — canonical schema, aliasing, units, sign, Coulomb counting |
| `src/battery_field_agent/registry.py` | release catalogue with licence and redistribution flags |
| `src/battery_field_agent/fetch.py` | checksum-verified fetch, atomic, resumable |
| `src/battery_field_agent/state.py` | idempotent unit-of-work state, atomic writes |
| `src/battery_field_agent/cli.py` | `list`, `inspect`, `normalize`, `status` |
| `docs/04-quota-budget.md` | the free-tier budget the system is engineered against |
| `.github/workflows/` | the orchestrator: build, ingest, dispatch |

## Status

Milestone 1 complete and tested: a single release goes from raw CSV to canonical
Parquet, verified independently by reading the artifact back with duckdb.

- 70 tests, stdlib `unittest`, no test framework required
- Core has **zero runtime dependencies**; Parquet output needs `duckdb`

Next: `docs/03-roadmap.md`.
