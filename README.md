# battery-field-agent

[![build-containers](https://github.com/KristofferHM/battery-field-agent/actions/workflows/build-containers.yml/badge.svg)](https://github.com/KristofferHM/battery-field-agent/actions/workflows/build-containers.yml)
![licence: MIT](https://img.shields.io/badge/licence-MIT-blue)
![python: 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![runtime deps: 0](https://img.shields.io/badge/runtime%20deps-0-brightgreen)

An agentic ingestion, normalisation and diagnostics pipeline for **public battery
field data** — the 226 GB / 131,096-cell public corpus — running entirely on
**free-tier cloud compute**.

The project's thesis is the constraint: this pipeline is designed for
**ephemeral, preemptible, quota-limited compute** (Kaggle 30 GPU-h/week, Colab
12-hour sessions, GitHub Actions as the free orchestrator). Designing around
death, preemption and a metered wall-clock budget is the engineering showcase.

## Status

Alpha — Milestone 1 (single-release ingest slice) is implemented and tested.

## Why

- Battery field data is published by 17+ research groups in incompatible formats,
  units and label conventions. Unifying it is unglamorous, necessary work that
  almost nobody does.
- Fault detection and health estimation on real field data (not lab cyclers) is
  where the industrial value is.
- The retrieval/agent layer over 23 heterogeneous releases is a genuine
  agent-automation problem, not a demo.

## Architecture

```
GitHub Actions  ──  orchestrator, cron, container builds   (free for public repos)
      │
      ├─ builds ingest image ──▶ GHCR
      ├─ CPU ingestion: idempotent, resumable, checkpointed
      └─ dispatches GPU work via the Kaggle API
                       │
                       ▼
        Kaggle kernels   ──  GPU workers (30 h/week, non-interactive)
        Colab            ──  interactive dev bench only, never a server
                       │
                       ▼
        Hugging Face Hub ──  state + artifacts (normalised Parquet, registry, results)
                       │
                       ▼
        HF Space (CPU)   ──  free demo / RAG query UI
```

See `docs/01-architecture.md` for the full design and `docs/04-quota-budget.md`
for the free-tier budget the system is engineered against.

## Install

Requires Python >= 3.10.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"   # .venv/bin/... on Linux/macOS
```

The **core is dependency-free** (stdlib only) so it runs anywhere. Parquet output
additionally needs `duckdb`; HTTP downloads work with stdlib `urllib` unless
`requests` is installed.

Use a pinned interpreter. This project was developed on a host where a bare
`python` resolved to different interpreters between invocations, which made the
Parquet tests silently skip. Pin the environment and the result is stable.

## Use

```bash
export PYTHONPATH=src        # or: pip install -e .

# What releases exist, and what may be redistributed
python -m battery_field_agent.cli list --policy

# Inspect an unknown file: preamble, delimiter, column mapping, units, sign convention
python -m battery_field_agent.cli inspect tests/fixtures/messy_mv_ma.csv --apply-sign

# Normalise to the canonical schema
python -m battery_field_agent.cli normalize tests/fixtures/messy_mv_ma.csv \
    -o out/canonical.parquet --source-id nasa_pcoe_randomized --cell-id B0005 \
    --state out/state.json

# Resume/status
python -m battery_field_agent.cli status --state out/state.json

# Independently verify a written artifact (reads it back with duckdb alone)
python scripts/verify_output.py
```

## Tests

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Stdlib `unittest` on purpose — no pytest requirement, so the suite runs in any
environment the pipeline runs in.

## Verified

Real output, not claims.

**Locally** (pinned venv, Python 3.12.12):

- **70 tests, 0 failures, 0 skips**.
- **70 tests, 0 failures, 3 skips** with third-party packages stripped from
  `sys.path` — the 3 skips are the duckdb-dependent Parquet tests, which *proves*
  the dependency-free-core claim rather than asserting it.
- A Parquet artifact written by the CLI was read back with **duckdb only** and
  reported `DOUBLE` for all six measurement columns, `BIGINT` for `cycle_index`
  and `step_index`, and correctly signed currents — `scripts/verify_output.py`
  ends in `RESULT: PASS`.
- Cross-check: the capacity integrated from the current trace equals the capacity
  column the instrument reported (0.05 Ah), so unit reconciliation *and* the sign
  flip are validated against an independent number.

**In CI** (GitHub Actions, Ubuntu, run
[38906764718](https://github.com/KristofferHM/battery-field-agent/actions/runs/38906764718) — `completed / success`, all three jobs):

| Job | Result |
|---|---|
| unit tests (stdlib path, no deps) | success |
| unit tests (duckdb present, parquet paths exercised) | success |
| build and push to GHCR | success |

The image was pushed, pulled and executed by the runner:

```
Digest: sha256:48d215f086f0aabb377ebea83ee83179b109e25d0bd677dd6006bcd0d9afe313
Status: Downloaded newer image for ghcr.io/kristofferhm/battery-field-agent:main
id                     kind   access             cells  redis  licence
----------------------------------------------------------------------
nasa_pcoe_randomized   lab    portal                28  yes    NASA open data (US public domain)
...
6 of 6 releases shown
```

The two test jobs also validate code developed on Windows against Linux, and the
`no deps` job runs the suite on a bare Python 3.12 with no third-party packages
at all.

### Bugs caught by execution, not review

Every one of these is now pinned by a test or a fix:

1. `cycle_index` / `step_index` were written as `VARCHAR` (downstream code would
   have received strings). Found by reading the Parquet back with duckdb.
2. `fail()` without a preceding `start()` did not advance the retry counter, so a
   permanently broken unit would have retried forever.
3. `docker/metadata-action` lowercases the owner; the hand-built `docker run`
   reference kept the capitals and docker rejected it as an invalid reference
   format. Found by the first real CI run.
4. `buildx` with `push: true` does not load the image into the local daemon, so
   the smoke test's bare `docker run` could not find it. Also found by CI.



## Repo layout

```
src/battery_field_agent/
  normalize.py   # canonical schema, aliasing, unit recovery, sign convention, Coulomb counting
  registry.py    # release catalogue with licence + redistribution flags
  fetch.py       # checksum-verified download / local placement
  state.py       # atomic, resumable job state
  cli.py         # list | inspect | normalize | status
tests/           # unittest suite + messy CSV fixtures
docs/            # architecture, datasets, roadmap, quota budget, licensing
.github/workflows/  # build containers · ingest · dispatch GPU
```

## Licence

Code: MIT. Data: **not redistributed** — each release keeps its own licence. See
`docs/05-licensing-and-risks.md`.
