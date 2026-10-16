# 05 — Licensing and risks

## Data licensing is per-release, and it is not uniform

The target corpus aggregates 23 releases from 17 groups. **Each keeps its own
licence.** Treat the collection as a directory, not as a licensed work.

Concrete positions already established:

| Release | Licence | May rows be republished? |
|---|---|---|
| NASA PCoE | NASA open data (US public domain) | yes |
| Severson MIT/Stanford | CC BY 4.0 — **attribution required** | yes, with citation |
| Oxford OBDD1 | CC BY 4.0 — **attribution required** | yes, with citation |
| CALCE CS2 | CALCE academic-use terms | **no** |
| Battery Archive | per-study, varies by contributor | **no** as a whole |
| Field corpus (23 releases) | per-release, unknown until imported | **assume no** |

### The policy the code enforces

```
raw data          -> never republished; the registry stores the landing page only
canonical parquet -> published ONLY when redistributable=True
derived metadata  -> publishable: schema maps, unit tables, quality reports
code + models     -> publishable (MIT)
```

`fetch.can_publish_raw()` is the single gate, and `bfa normalize --public`
**refuses to write** (exit code 3) when it returns false. This is enforced in
code rather than left to discipline, because the failure mode — accidentally
mirroring somebody's dataset into a public repo — is not recoverable by deleting
a file.

### What is still publishable for a non-redistributable release

Process it locally, publish the **metrics**: coverage, observed unit distribution,
sign convention detected, unmapped columns, fault counts, model performance.
Numbers about the data are yours; the data is not.

**Attribution is not optional for the CC BY 4.0 releases.** Any published result
derived from Severson or Oxford data must cite the original papers. Where a
dataset card is published, it must carry the citation.

## Risks, ranked by how likely they are to bite

### 1. Format heterogeneity exceeds the CSV assumption — likely

M1 handles CSV. NASA ships `.mat`, several field releases are HDF5 or vendor
binaries. **Mitigation:** reader layer added in M2; the reconciliation core is
already format-agnostic (it takes rows, not files), so only the reader changes.

### 2. "Field data" contains less signal than hoped — likely, partly

Field data is dominated by partial cycles, long rests and uncontrolled
temperature. Full-discharge capacity, the standard health metric, may simply not
exist for most systems. **Mitigation:** M4 is built around capacity estimation
*without* complete discharges, and ambient regression is mandatory rather than
optional. If the honest finding is "this corpus cannot support the claim", that
is a result and gets written down.

### 3. Quota exhaustion — likely, and planned for

Kaggle's 30 GPU-h/week is metered wall-clock and floats. **Mitigation:**
`docs/04-quota-budget.md` plans for 20 h, not 30; every unit is checkpointed;
CPU work is kept on Actions where it is free.

### 4. Free-tier terms change — possible

Kaggle and Colab both reserve the right to change quotas; Colab already
restricts free users' GPU access. **Mitigation:** the orchestrator is GitHub
Actions, which is free for public repos on a published schedule, and the design
has no hard dependency on any single compute surface. The GPU layer is
replaceable; the schema and the state are not tied to it.

### 5. Licence violation by accident — low likelihood, high severity

See the gate above. **Mitigation:** code-enforced refusal, not a convention.

### 6. Compute is not the only bottleneck, attention is — likely

A decade-old laptop and phone-only access mean long interactive sessions are
expensive. **Mitigation:** everything runs unattended through Actions and the
Kaggle API; the human is needed to review, not to babysit.

## Honest assessment of the moat

There is none in the code. Normalisation logic is reproducible by anyone who
reads this repo, and that is fine — the project is not a product.

What it produces that cannot be reverse-engineered is **evidence**: a working
pipeline, a reconciled 23-release schema with a documented decision per
convention, a measured diagnostic result, and a policy that beats the baseline
under an explicit cost model. Those are the artifacts that matter, and they are
artifacts of having done the work.
