# 06 — Model prototyping plan

This is the plan for the modelling phase, to run **once the frameworks and
backends are in place** (M2 corpus + M3 ingestion agent complete). It is
deliberately written before any results exist, because two things must be frozen
in advance or the results mean nothing: **the splits** and **the metric**.

## Scope: two models, sequenced

| | Model | Type | Role |
|---|---|---|---|
| **A** | Health / fault-risk estimator | supervised + time-to-event | produces the belief the scheduler acts on |
| **B** | Diagnostic scheduler | reinforcement learning | decides when to spend an expensive test |

**B depends on A.** The scheduler's state is A's posterior, not raw telemetry.
A well-calibrated probability is the point of A; a good ranking is not enough.

## 0. Preconditions — what "in place" must actually mean

Do not start modelling until every box is ticked. Each one exists to stop a
specific way of fooling yourself.

- [ ] M2 complete: all field releases in canonical Parquet on HF Hub, one dataset
      card per release, unmapped columns documented per release
- [ ] M3 complete: ingestion runs unattended and the Coulomb cross-check gates
      every release automatically
- [ ] **Feature table materialised and versioned** — one row per
      `(system, cycle-window)`, with a hash so a result can name its input
- [ ] **Split file committed before any modelling** — grouped by system, frozen
- [ ] Ambient temperature series joined (SMHI / ERA5) for every site
- [ ] **Evaluation harness + metric definition committed before any result**
- [ ] Fault-label semantics written down (see 2.3) and reviewed by a human

If the split or the metric can move after seeing results, the experiment is
decorative. Freeze both, in git, first.

## 1. Define the decision precisely

State it as an operational question, not a modelling one:

> Given a fleet of deployed systems observed only through partial telemetry, at
> what point is it worth spending a capacity test — which costs cycles and
> downtime — on each unit?

- **Decision unit:** one system, one decision epoch (e.g. weekly)
- **Horizon:** until end of life or end of observation
- **Cost:** a capacity test consumes real cycles and real downtime; both must be
  stated as numbers, not hand-waved
- **Explicit non-goal:** beating published lab SOH benchmarks. Crowded, and not
  the question.

## 2. Data preparation

### 2.1 Unit of analysis

A `(system, cycle-window)` table, **not** raw samples. Raw sample rows are the
input to feature extraction; the model never sees them. Aggregate to a window of
k cycles (sweep k ∈ {1, 5, 20}) and re-run the cheap baselines per k.

### 2.2 Feature families (each with the physics it encodes)

| Family | Examples | Why |
|---|---|---|
| Capacity proxy | partial-cycle capacity, rest-relaxation estimate | the health signal; often the only one available |
| Resistance proxy | ΔV/ΔI at a matched SOC point | power fade, separable from capacity fade |
| ICA / DVA | peak position, height, area shift | reveals degradation *mode*, not just amount |
| Efficiency | coulombic, energy, round-trip | lithium inventory loss, parasitic reactions |
| Duty | DoD distribution, mean SOC, C-rate histogram, rest fraction | explains *why* the same cell ages differently |
| Thermal | degree-hours, time above threshold, thermal cycles | Arrhenius driver; without it "degradation" is mostly summer |
| Age | calendar days, cumulative Ah, equivalent full cycles | the throughput axis |

### 2.3 Labels — and the trap in them

784 labelled faults. Three things must be settled before modelling:

1. **`reported fault` ≠ `fault occurred`.** Reporting is a human process with its
   own bias. Audit a sample of labels against the telemetry and state the
   agreement rate. If it is poor, that is a headline finding, not a footnote.
2. **Right-censoring.** A system still running has not failed *yet*. Treating
   "no fault observed" as a clean negative is wrong and biases the model toward
   optimism. This makes the problem **time-to-event**, not binary classification.
3. **Imbalance.** Faults are rare. Use survival framing and/or class weights, and
   report precision/recall at a fixed alert budget rather than raw accuracy.

### 2.4 Splits — the leakage trap

- **Group by system, and group by site.** A random row split leaks one unit's
  idiosyncrasies into test and inflates every metric.
- **Temporal holdout** as a second axis: train on early, test on late.
- Cross-site generalisation is the interesting generalisation. Report it
  separately from same-site.
- The split file is generated once, committed, and never regenerated silently.

### 2.5 Mandatory leakage audit

For every feature, ask: *would this value have been available at the decision
epoch?* Anything using post-decision data must be dropped. This is the single
most common way a battery-degradation result becomes fiction.

## 3. Baselines before models — mandatory

Compute and publish all four. These define "working".

1. **Fixed-interval testing** (every N cycles). The policy to beat.
2. **Threshold on a naive proxy** (e.g. capacity proxy < X).
3. **Gradient boosting** on the feature set — supervised, fast, strong.
4. **Survival model** (Cox / gradient-boosted survival) for time-to-fault.

If the RL scheduler cannot beat #1 and #4, that is the result. Report it.

## 4. Model A — the estimator

- **Stage A1:** regression on the capacity proxy, per cycle-window. Validate
  against the proxy only, and say so — the proxy is not ground truth.
- **Stage A2:** time-to-fault. Metrics: **C-index**, time-dependent AUC, and
  **calibration** (reliability curves / Brier score).
- **Calibration is non-negotiable.** The scheduler consumes probabilities; a
  well-ranked but miscalibrated model produces a policy that tests too much or
  too little.
- **Compute:** CPU or Colab. `scikit-learn`, `scikit-survival` / `lifelines`.
  Free tier is ample — do not spend GPU quota here.

## 5. Model B's environment — the simulator

**Why a simulator at all:** you cannot run experiments on a customer's field
battery. Either learn from logs (offline RL) or fit an environment to the data.
Fit an environment — the corpus has enough cycling to support a semi-empirical
degradation model, and a simulator allows counterfactual evaluation that logs
alone do not.

- **Form:** capacity fade as a function of throughput, DoD, C-rate and
  temperature with an Arrhenius term; resistance growth modelled in parallel; a
  stochastic fault hazard conditioned on health state.
- **Fit per chemistry family** (LFP / NMC / NCA) — never pool them.
- **Simulator validation is its own deliverable:** one-step-ahead error *and*
  long-horizon trajectory error on held-out systems. Report both; a simulator
  that tracks one step but drifts over a lifetime is not usable.
- **Hold out a variant simulator** (perturbed parameters, or a chemistry excluded
  from fitting) purely for policy evaluation. A policy that only wins inside the
  simulator it was trained on has learned the simulator, not the problem.

## 6. Model B — the RL prototype

### 6.1 Formulation

- **State:** estimator's health belief (mean + uncertainty), cycles since last
  test, duty summary, thermal history, hazard estimate
- **Observation is partial and noisy** — this is a POMDP; the belief is what the
  agent conditions on. That framing is the reason RL is the right tool here
  rather than a threshold.
- **Action:** `{test now, defer one epoch, flag for inspection}` (3 actions)
- **Reward:** detection benefit − test cost − missed-fault penalty, with the cost
  term expressed in the same units as the benefit

### 6.2 Algorithm ladder — start small, climb only if needed

1. **Tabular Q-learning** on a discretised state — sanity check and an approximate
   upper bound within the discretisation
2. **Fitted Q-Iteration** with a small function approximator
3. **Offline RL** (CQL / IQL) if learning directly from logged behaviour
4. Only then consider deep on-policy methods — and expect to conclude they are
   unnecessary

> **Do not start with PPO.** Three discrete actions, a low-dimensional state and a
> 20 GPU-hour weekly budget. Deep on-policy RL is the wrong tool here, and the
> quota is too scarce to prove that empirically.

### 6.3 Training budget

Small nets over tabular state: **minutes on CPU**, comfortably under an hour on a
T4. Therefore run **many seeds cheaply** — target 10 seeds × 5 configurations
inside a single Kaggle session, rather than one long run.

### 6.4 Reward sensitivity as a result

Sweep the test cost across its plausible range and report where the learned
policy switches from fixed-interval to event-driven. That curve is more
transferable than any single policy.

## 7. Evaluation protocol (pre-registered, committed before the first run)

- **Primary:** cost-adjusted detection — faults detected with lead time ≥ L, per
  test-hour consumed, on held-out **systems**
- **Secondary:** median lead time, false-alarm rate, tests per unit-year
- **Uncertainty:** bootstrap CIs resampled over **systems**, not samples. With
  ~3,025 systems the system count is the effective sample size, not the row count.
- **Comparison:** paired against the baselines on identical systems
- **Fidelity check:** the same policy evaluated on the **held-out simulator
  variant**
- **Offline-RL check:** if learning from logs, compare an IPS/DR off-policy
  estimate against simulated truth. If they disagree badly, trust neither.

## 8. Ablations

| Ablation | Question it answers |
|---|---|
| Drop uncertainty from state | does the POMDP framing earn its complexity? |
| Drop thermal features | how much is "degradation" really seasonality? |
| Drop the missed-fault penalty | is the policy just minimising test count? |
| Estimator → oracle health | upper bound: how much is left for the scheduler? |
| Sweep simulator fidelity | does a worse environment produce a worse policy? |

## 9. Kill criteria and pivots — decide now, not mid-project

- **Labels too noisy** (audit agreement below threshold) → pivot to unsupervised
  anomaly detection and report the label-audit result as the finding.
- **No full discharges anywhere** → the capacity proxy *is* the project; validate
  it or stop.
- **RL wins only inside its training simulator** → not a result. Fix the fidelity
  check or publish the negative finding.
- **A simple threshold heuristic matches RL within CI** → report that. It is more
  useful to a maintenance planner and more honest, and it still demonstrates the
  method.
- **The corpus cannot support the claim** → say so in writing. A negative result
  with a clean audit is a real deliverable; a positive one with a leaky split is
  not.

## 10. Execution mapped to free compute

| Step | Surface | Why there | Budget |
|---|---|---|---|
| Feature extraction | Actions (CPU) | no GPU benefit | free |
| Baselines + estimator | Colab / CPU | scikit-learn, cheap | free |
| Simulator fitting | Actions (CPU) | optimisation, not deep learning | free |
| RL training sweeps | Kaggle GPU | small nets, many seeds | ≤ 6 h/run, ≤ 3 runs/week |
| Ablations | Kaggle GPU | reuse the harness | within the same ceiling |
| Write-up | local | — | — |

Against a **20 h/week planned ceiling** (of a 30 h allowance), a full sweep
campaign fits in one week with retry headroom.

## 11. Deliverables

1. Feature table + frozen split file, both versioned
2. Baseline table (4 baselines, paired, with CIs)
3. Estimator with calibration curves and a leakage audit
4. Simulator + its validation report
5. RL policy: learning curves across seeds, comparison table, fidelity check
6. Ablation table
7. A write-up that states explicitly **what cannot be concluded**
