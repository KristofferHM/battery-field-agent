"""Catalogue of the public battery releases this pipeline can ingest.

Two important properties are recorded per entry:

* **licence** and **redistributable**, because derived artifacts may be
  publishable while raw data may not. The pipeline reads this flag before it
  writes anything to a public artifact store.
* **access**, because the mechanics differ: some releases are a direct HTTP
  download, some sit behind a portal or a licence click, some ship as MATLAB
  ``.mat`` and some as CSV/ZIP.

The large *field-data* collection (23 releases, 226 GB, 131,096 cells) is
registered here as a single umbrella entry pending import from its own registry
repository -- see ``docs/02-datasets.md``. The named lab-side releases below are
concrete, individually addressable and usable today.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Iterable

__all__ = ["Release", "RELEASES", "get", "filter_releases", "DISTRIBUTION_POLICY"]


@dataclass(frozen=True)
class Release:
    """One addressable public battery dataset."""

    id: str
    name: str
    kind: str
    """``lab`` = controlled cycler study, ``field`` = deployed system telemetry."""
    access: str
    """``http`` | ``portal`` | ``licence_click`` | ``registry_import``"""
    landing_page: str
    format: str
    licence: str
    redistributable: bool
    cells: int | None = None
    chemistry: str | None = None
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)


RELEASES: tuple[Release, ...] = (
    Release(
        id="nasa_pcoe_randomized",
        name="NASA PCoE Randomized Battery Usage (sets 1-7)",
        kind="lab",
        access="portal",
        landing_page="https://www.nasa.gov/intelligent-systems-division/discovery-and-systems-health/pcoe/pcoe-data-set-repository/",
        format="mat/csv/zip",
        licence="NASA open data (US public domain)",
        redistributable=True,
        cells=28,
        chemistry="18650 NMC/LCO",
        notes=(
            "Random-walk current profiles -- unusual and valuable: most lab studies "
            "use constant or drive-cycle loads, so this is the closest lab analogue "
            "to field duty. Good first target for the reference implementation."
        ),
        tags=("random-walk", "reference-implementation", "beginners-ok"),
    ),
    Release(
        id="severson_mit_stanford",
        name="Severson et al. 124-cell fast-charging dataset (MIT/Stanford/Toyota)",
        kind="lab",
        access="licence_click",
        landing_page="https://data.matr.io/1/projects/5c48dd2bc625d700019f3204",
        format="mat/csv",
        licence="CC BY 4.0 (attribution required)",
        redistributable=True,
        cells=124,
        chemistry="A123 APR18650M1A LFP/graphite",
        notes=(
            "The canonical fast-charging dataset: 72 charging protocols, full "
            "lifetime V/I/T at 1 Hz, and end-of-life labels. Attribution is "
            "mandatory, so cite it in any published derived artifact."
        ),
        tags=("fast-charging", "1hz", "labelled-eol"),
    ),
    Release(
        id="oxford_obdd1",
        name="Oxford Battery Degradation Dataset 1 (OBDD1)",
        kind="lab",
        access="portal",
        landing_page="https://ora.ox.ac.uk/objects/uuid:03ba4b01-cfed-46d3-9b1a-7d4a7bdf6fac",
        format="mat",
        licence="CC BY 4.0 (attribution required)",
        redistributable=True,
        cells=8,
        chemistry="Kokam SLPB533459H4 (740 mAh pouch)",
        notes=(
            "Artemis Urban drive cycle at 40 C, characterised every 100 cycles. "
            "Small enough to iterate on in a 12-hour Kaggle session, which makes "
            "it the right smoke test before the 226 GB corpus."
        ),
        tags=("drive-cycle", "small", "smoke-test"),
    ),
    Release(
        id="calce_cs2",
        name="CALCE CS2 (University of Maryland)",
        kind="lab",
        access="portal",
        landing_page="https://calce.umd.edu/data#CS2",
        format="xlsx/zip",
        licence="CALCE academic-use terms",
        redistributable=False,
        cells=4,
        chemistry="18650 LCO",
        notes="Small, well-documented, widely cited baseline. Check terms before republishing anything derived.",
        tags=("baseline", "small"),
    ),
    Release(
        id="battery_archive",
        name="Battery Archive (aggregator: HNEI, ISU-ILCC, Sandia, Oxford, ...)",
        kind="lab",
        access="registry_import",
        landing_page="https://www.batteryarchive.org/",
        format="csv/parquet",
        licence="per-study; varies by contributing institution",
        redistributable=False,
        cells=None,
        chemistry="mixed (NMC, NCA, LFP)",
        notes=(
            "Pre-harmonised across contributors with a documented schema -- the "
            "best available example of the target schema this project converges "
            "on. Licence varies per study, so treat redistributability as per-study."
        ),
        tags=("aggregator", "schema-reference"),
    ),
    Release(
        id="field_corpus_2026",
        name="Public Battery Field Data collection (226 GB, 23 releases, 17 groups)",
        kind="field",
        access="registry_import",
        landing_page="https://arxiv.org/abs/2610.05811",
        format="mixed (23 incompatible formats)",
        licence="per-release; must be read from the collection registry",
        redistributable=False,
        cells=131096,
        chemistry="mixed, deployed systems",
        notes=(
            "THE target corpus. 3,025 systems, 44.4 MWh, 2,918 unit-years, 784 "
            "labelled faults. Its authors published a metadata registry precisely "
            "because every release carries its own format, units and labels. "
            "Import that registry into this catalogue as milestone 2; until then "
            "this entry is a placeholder and the lab-side releases above are the "
            "test surface."
        ),
        tags=("target-corpus", "faults", "heterogeneous", "registry-pending"),
    ),
)

DISTRIBUTION_POLICY = """artifact distribution policy

  raw data        -> never republished; referenced by landing_page only
  canonical parquet -> published ONLY when redistributable=True
  derived metadata  -> publishable: schema maps, unit tables, quality reports
  code + models     -> publishable (MIT)

Every write to a public artifact store must check `redistributable` first. A
release with redistributable=False can still be processed locally and its
*metrics* published -- just not its rows.
"""


def get(release_id: str) -> Release:
    for rel in RELEASES:
        if rel.id == release_id:
            return rel
    known = ", ".join(r.id for r in RELEASES)
    raise KeyError(f"unknown release {release_id!r}; known: {known}")


def filter_releases(
    kind: str | None = None,
    redistributable_only: bool = False,
    tag: str | None = None,
) -> list[Release]:
    out: Iterable[Release] = RELEASES
    if kind:
        out = [r for r in out if r.kind == kind]
    if tag:
        out = [r for r in out if tag in r.tags]
    if redistributable_only:
        out = [r for r in out if r.redistributable]
    return list(out)


def render_table(releases: Iterable[Release]) -> str:
    rows = list(releases)
    if not rows:
        return "no releases matched"
    widths = {
        "id": max(len("id"), *(len(r.id) for r in rows)),
        "kind": max(len("kind"), *(len(r.kind) for r in rows)),
        "access": max(len("access"), *(len(r.access) for r in rows)),
        "cells": 7,
        "redis": 5,
    }
    head = (
        f"{'id':<{widths['id']}}  {'kind':<{widths['kind']}}  "
        f"{'access':<{widths['access']}}  {'cells':>{widths['cells']}}  {'redis':<{widths['redis']}}  licence"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r.id:<{widths['id']}}  {r.kind:<{widths['kind']}}  "
            f"{r.access:<{widths['access']}}  {('' if r.cells is None else r.cells):>{widths['cells']}}  "
            f"{('yes' if r.redistributable else 'NO'):<{widths['redis']}}  {r.licence}"
        )
    return "\n".join(lines)
