# 02 — Datasets

## The target corpus

**Public Battery Field Data** — a curated collection of **23 public field-data
releases from 17 research groups**: **226 GB**, **131,096 Li-ion cells**, **3,025
battery systems**, **44.4 MWh**, **2,918 unit-years** of observation, and **784
labelled/reported faults**. Released with a metadata registry, checksums and
code, and framed by its authors as a platform for battery field diagnostics,
fault detection and health estimation.

Paper: <https://arxiv.org/abs/2610.05811>

Why it is the right target:

- **Field, not lab.** Duty cycles are real: partial cycling, rest periods,
  temperature that nobody controlled, and faults that were not induced on purpose.
- **Barely touched.** The collection is days old at time of writing. The authors
  built the metadata registry; nobody has built the diagnostic layer on top.
- **Heterogeneous on purpose.** Its defining difficulty — 23 incompatible
  formats, units and label conventions — is exactly what this pipeline's core
  exists to absorb, so the corpus validates the architecture rather than merely
  being processed by it.

**Licensing caveat:** licence is **per-release**. Do not assume the collection's
licence covers redistribution. See `docs/05-licensing-and-risks.md`.

## Lab-side releases (available now, used as the test surface)

| id | Cells | Chemistry | Access | Licence | Redistributable |
|---|---|---|---|---|---|
| `nasa_pcoe_randomized` | 28 | 18650 NMC/LCO | portal | NASA open data | yes |
| `severson_mit_stanford` | 124 | A123 LFP/graphite | licence click | CC BY 4.0 | yes (attribution) |
| `oxford_obdd1` | 8 | Kokam 740 mAh pouch | portal | CC BY 4.0 | yes (attribution) |
| `calce_cs2` | 4 | 18650 LCO | portal | CALCE academic terms | **no** |
| `battery_archive` | — | mixed | registry import | per-study | **no** |

`battery_archive` is worth studying even though its rows are not republishable:
it is the closest existing example of the harmonised schema this project
converges on, and it documents the per-contributor quirks that motivate the
alias map.

### Why NASA Randomized is the reference implementation

Most lab studies use constant current or a standard drive cycle. The NASA
Randomized Battery Usage sets use **random-walk current profiles**, which is the
closest lab analogue to uncontrolled field duty. It is also small enough to
iterate on in a single session, and it ships as `.mat`, which forces the
reader layer to handle a non-CSV container early rather than late.

## Companion sources (no cells, but needed for the diagnostics layer)

| Source | Provides | Access |
|---|---|---|
| SMHI open data | Swedish meteorological + oceanographic series, free API | open |
| Open-Meteo historical archive | gridded temperature history for site ambient correction | open API |
| Copernicus / ERA5 | reanalysis ambient conditions for field sites | open |

Field capacity fade is heavily confounded by ambient temperature. Without an
ambient series a "degradation" signal can be dominated by summer. Any claim about
field degradation that does not regress against site temperature is not a claim.

## Schema this project converges on

One row per measurement sample:

| Column | Type | Meaning |
|---|---|---|
| `source_id` | VARCHAR | release id from the registry |
| `cell_id` | VARCHAR | cell identity within the release |
| `cycle_index` | BIGINT | cycle number |
| `step_index` | BIGINT | step within the cycle |
| `step_type` | VARCHAR | raw label as published |
| `step_family` | VARCHAR | normalised: `charge` / `discharge` / `rest` / null |
| `timestamp_s` | DOUBLE | elapsed seconds from the cell's first sample |
| `voltage_v` | DOUBLE | volts |
| `current_a` | DOUBLE | amperes, **positive == discharge** |
| `temperature_c` | DOUBLE | degrees celsius |
| `charge_capacity_ah` | DOUBLE | ampere-hours |
| `discharge_capacity_ah` | DOUBLE | ampere-hours |

Two conventions are load-bearing and worth stating in a dataset card:

1. **`current_a` is signed, positive is discharge.** Roughly half of published
   releases do the opposite. An unsigned corpus is not usable for diagnostics.
2. **`timestamp_s` is per-cell elapsed.** Absolute timestamps are shifted to a
   per-cell zero, because field releases span years and lab timestamps may be
   wall-clock with gaps.
