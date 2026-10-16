# 04 — Free-tier quota budget

The scarcest resource in this project is not money and not disk. It is **metered
GPU wall-clock that resets weekly**. This file exists so that budget is designed
against rather than discovered by failure.

## What is actually available (verified, October 2026)

### Kaggle — the GPU worker

| Resource | Value | Note |
|---|---|---|
| GPU quota | **30 h/week** | resets Sunday 00:00 UTC; shown in account settings |
| Session cap | **12 h** CPU/GPU, 9 h TPU | hard |
| Idle timeout | **60 min** | interactive sessions; stop them manually to save quota |
| Working disk | **20 GB** auto-saved in `/kaggle/working` | |
| Accelerator | 1× P100 16 GB **or** 2× T4 (2×16 GB) | menu varies |
| TPU | 20 h/week, v3-8, 9 h/session | |
| Requirement | **phone verification** to unlock accelerators | do this first |

**Critical:** quota counts **session wall-clock, not GPU utilisation**. An idle
notebook with a GPU attached burns the budget at full rate. Kaggle's own docs
recommend driving runs through the **API** to avoid interactive sessions entirely.

### Google Colab — the dev bench only

| Resource | Value |
|---|---|
| Session cap | 12 h; ~90 min idle disconnect (unpublished, commonly reported) |
| GPU | T4 16 GB when available — **not guaranteed**; Google names no model |
| GPU quota | dynamic, commonly reported 15–30 h/week, **unpublished** |
| RAM | ~12–13 GB (standard profile; high-RAM is paid) |
| Disk | **wiped when the session ends** |

**Colab cannot be a server.** Without a compute-unit balance, Google's FAQ
disallows SSH shells, remote desktops, and using a web UI in place of the
notebook. Treat it as a place to write and debug, nothing more.

### GitHub Actions — the orchestrator

| Resource | Value |
|---|---|
| Minutes, **public** repos | **free** (standard runners) |
| Minutes, private repos (Free plan) | 2,000/month |
| GitHub Packages / GHCR, public repos | free |
| Artifact storage (Free plan) | 500 MB — **small; use HF Hub for anything larger** |
| Cache | 10 GB per repo |

### Hugging Face Hub — the state and artifact store

| Resource | Value |
|---|---|
| Public repos | unlimited, best-effort storage |
| Private repos | 100 GB |
| Spaces, CPU runtime | 8 units running concurrently, free |
| ZeroGPU | 5 min/day free |
| Jobs & Scripts | **pay-as-you-go — not free** |

## The weekly budget

| Workload | Surface | Budget | ~Cost per run |
|---|---|---|---|
| Ingestion, normalisation, QC | Actions (CPU) | free | minutes |
| Container build | Actions → GHCR | free | ~2–4 min on cache miss |
| Environment verification | Actions (CPU) | free | seconds |
| RL training / diagnostics | Kaggle GPU | 30 h/week | aim ≤ 6 h/run, ≤ 3 runs/week |
| Interactive dev + debug | Colab | ~15–30 h/week | keep sessions short; **stop them** |
| Demo / RAG UI | HF Space CPU | 8 concurrent free | always-on |

Headroom policy: **plan for 20 h/week of Kaggle GPU, not 30.** The quota floats,
GPU availability is not contractual, and a plan with no slack breaks the first
week a run needs a retry.

## Rules the pipeline follows

1. **Never keep an interactive Kaggle session open.** Stop it before closing the
   tab — Kaggle documents up to 60 min of compute saved per session.
2. **Dispatch GPU work through the API**, not by hand, so runs are reproducible
   and auditable.
3. **Put small things on Actions, not GPUs.** Reconciliation is CPU; pandas and
   scikit-learn do not benefit from a GPU at all, so using one is pure waste.
4. **Checkpoint every unit.** A unit that costs 40 min of GPU time must be
   resumable, or a preemption costs 40 min of a 30-hour weekly allowance.
5. **Prefer the small model.** An RL scheduler over tabular state needs no deep
   network. Choosing a small policy is a budget decision, not a compromise.
6. **Shrink before you scale.** Run on `oxford_obdd1` (8 cells) first; only touch
   the 226 GB corpus once the pipeline is stable on the small release.
7. **Artifacts go to HF Hub, not Actions.** 500 MB of Actions artifact storage is
   smaller than a single release.

## What this constraint buys

Designing for death, preemption and a metered budget produces the same properties
production ML platforms need: idempotent work units, external state, checkpointed
progress, cost-aware scheduling, and a scheduler that survives the compute it
schedules onto. That is the engineering being demonstrated.
