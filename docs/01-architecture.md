# 01 — Architecture

## The constraint is the design

The pipeline may not assume a server. Every compute surface available to it
**dies on a timer**:

| Surface | Dies | Notes |
|---|---|---|
| Kaggle kernel | 12 h max; 60 min idle timeout | 30 GPU-h/week, counts **wall-clock**, not utilisation |
| Colab free | 12 h max; ~90 min idle | VM disk wiped; SSH/remote desktop **disallowed** without compute units |
| GitHub Actions runner | job timeout | free for **public** repos |
| Laptop | lid, sleep, battery | decade-old CPU — a dev bench, not a worker |

So the architecture is not "services" but **idempotent units of work plus an
external record of which units finished**. Nothing is stateful; everything is
resumable; the state file is the only thing that must survive.

## Topology

```
                    ┌──────────────────────────────────────────┐
                    │  GitHub Actions  (orchestrator, free)     │
                    │  · cron schedules                         │
                    │  · builds + pushes images to GHCR         │
                    │  · runs CPU ingestion jobs                │
                    │  · dispatches GPU work via the Kaggle API │
                    └───────────────┬──────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────────┐
              ▼                     ▼                         ▼
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
   │ GHCR             │  │ Kaggle kernels   │  │ Hugging Face Hub     │
   │ ingest image     │  │ GPU workers      │  │ state + artifacts    │
   │ (one, parameter- │  │ 30 h/week        │  │ normalised Parquet   │
   │  ised per        │  │ non-interactive  │  │ registry, metrics    │
   │  release)        │  │ via kaggle CLI   │  │ unlimited public     │
   └──────────────────┘  └──────────────────┘  └──────────┬───────────┘
                                                          │
                                              ┌───────────▼───────────┐
                                              │ HF Space (CPU, free)  │
                                              │ demo / RAG query UI   │
                                              └───────────────────────┘

   Colab = interactive dev bench ONLY. Never a server, never a scheduled worker.
```

## The unit of work

Everything long-running is decomposed into a unit that can be re-run safely.
A unit for this pipeline is **one release × one cell × one time window**, keyed
by a stable id.

```python
st = state.load("state.json")
for unit_id in st.pending(all_unit_ids):        # skips done, caps retries
    st.start(unit_id)
    try:
        out = process(unit_id)
        st.finish(unit_id, rows_out=len(out), output=path)
    except Exception as exc:
        st.fail(unit_id, str(exc))
    state.save(st, "state.json")                 # atomic, after every unit
```

Two properties fall out of this for free:

- **Preemption is free.** A killed runner loses at most one unit; the next run
  picks up from `st.pending(...)`.
- **Re-runs are cheap.** `fetch_url` checks the target's hash before opening a
  socket, so a resumed job does not re-download 226 GB.

## Why one image, parameterised

Project 1 in the original brief was a containerised benchmark harness for
materials ML models, motivated by a documented barrier: *every model needs its
own isolated stack and specific library versions*. The same problem appears here
in a concrete form — 23 releases, incompatible formats and units — but the
answer is **not** 23 images.

One image that exposes the reconciliation core, with the per-release differences
expressed as **data** (`registry.py`, the unit tables, the alias map), keeps the
environment count at one and makes the differences reviewable in a diff. Adding
a release is a registry entry plus, at most, an alias, not a Dockerfile.

## Data flow for one release

```
landing page / mirror
   │  fetch.py        checksum verified, atomic, resumable, never partial
   ▼
raw/                 (never republished unless redistributable=True)
   │  read_table()    delimiter + header-row detection past metadata preamble
   │  resolve_columns aliases, unit recovery from header text, --map overrides
   │  inspect         decision report shown BEFORE anything is trusted
   ▼                                       ▲
   │  normalize_records  unit transforms, ISO/epoch timestamps → elapsed s   │
   │  infer_sign_convention  discharge-positive canonicalisation             │
   │  integrate_capacity  independent Coulomb-count cross-check ─────────────┘
   ▼
canonical.parquet    DOUBLE measurements, BIGINT indices, VARCHAR labels
   │
   ├─▶ HF Hub dataset   (if redistributable)   /   metrics only (if not)
   └─▶ diagnostics + RL layer  (Kaggle GPU)
```

## The layer that does not exist yet

Milestone 1 stops at canonical Parquet on purpose. The three layers above it are
specified in `docs/03-roadmap.md` and each maps to one of the required
capabilities:

| Layer | Capability | Where it runs |
|---|---|---|
| Ingestion agent | agent automation | Actions (CPU) |
| Quality/anomaly agent | RAG over release papers → the agent reads its own ingestion report | Actions (CPU) |
| Diagnostic scheduler | **RL** — when to spend a capacity test | Kaggle (GPU, small) |
| Search/briefing agent | agents + RAG + orchestration | Actions → HF Space |

## Failure modes this design accepts

| Failure | Response |
|---|---|
| Runner killed mid-unit | state file re-reads; at most one unit lost |
| Kaggle quota exhausted | jobs queue to next week; Actions CPU work continues |
| Download corrupted | checksum mismatch → temp file discarded, unit fails, retry |
| Column unmapped | `inspect` prints it and exits 2 **before** a bad dataset is written |
| Non-redistributable source | `--public` refuses the write and exits 3 |
| Unit permanently broken | retry cap in `meta.max_attempts` stops the loop |

## Deliberate non-goals

- **No database.** Files plus atomic JSON beat a service you must keep alive.
- **No Kubernetes.** A scheduler that outlives its compute is the thing this
  project cannot afford; Actions *is* the scheduler.
- **No pyarrow.** `duckdb` writes Parquet in the container, keeping the image
  small and the dependency graph shallow.
