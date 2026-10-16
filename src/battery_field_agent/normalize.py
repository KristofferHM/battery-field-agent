"""Canonicalise heterogeneous battery cycling data into one schema.

The core is deliberately **dependency-free**. Header aliasing, unit recovery,
sign-convention inference and Coulomb counting are small deterministic rules
that must behave identically on a laptop, inside the ingest container and inside
a Kaggle kernel -- none of which share a scientific Python stack. Anything
heavier (Parquet, pandas) is delegated to an optional writer layer.

Public field data is messy in predictable ways, and those ways are what this
module exists to absorb:

* a variable number of metadata preamble lines before the real header
* comma / semicolon / tab delimiters, sometimes mixed inside one corpus
* units carried in the header (``Voltage(mV)``, ``current_mA``, ``Time_ms``)
* the same physical quantity under a dozen names (``Ecell``, ``V``, ``voltage``)
* sign conventions: some sources log discharge as positive, others negative
* temperatures in either celsius or kelvin
* timestamps as elapsed seconds, milliseconds, or ISO datetimes
* capacity columns in Ah or mAh, or missing entirely
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

__all__ = [
    "CANONICAL_FIELDS",
    "NUMERIC_FIELDS",
    "INT_FIELDS",
    "ColumnMap",
    "SignInference",
    "norm_key",
    "parse_header",
    "unit_transform",
    "resolve_columns",
    "detect_delimiter",
    "find_header_row",
    "read_table",
    "normalize_records",
    "infer_sign_convention",
    "apply_sign_convention",
    "integrate_capacity",
    "looks_like_epoch",
    "to_elapsed_seconds",
    "validate_record",
    "validate_series",
    "summarise",
    "write_records",
]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

CANONICAL_FIELDS: tuple[str, ...] = (
    "source_id",
    "cell_id",
    "cycle_index",
    "step_index",
    "step_type",
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "charge_capacity_ah",
    "discharge_capacity_ah",
)

NUMERIC_FIELDS: tuple[str, ...] = (
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "charge_capacity_ah",
    "discharge_capacity_ah",
)

INT_FIELDS: tuple[str, ...] = ("cycle_index", "step_index")

#: Plausibility envelope for a single Li-ion cell. Values outside these bounds
#: are almost always a unit error or a sensor fault, not a real measurement --
#: which makes this the first cheap diagnostic in the pipeline.
REALISTIC_RANGES: dict[str, tuple[float, float]] = {
    "voltage_v": (0.0, 6.0),
    "current_a": (-1000.0, 1000.0),
    "temperature_c": (-60.0, 120.0),
    "charge_capacity_ah": (0.0, 1000.0),
    "discharge_capacity_ah": (0.0, 1000.0),
}

#: Canonical sign convention: **positive current == discharge**.
CONVENTIONS = ("positive_discharge", "negative_discharge", "unsigned")

#: Label families used to detect which step is which.
DISCHARGE_LABELS = frozenset(
    {"discharge", "dchg", "dch", "dis", "dsch", "discharging", "dc", "d"}
)
CHARGE_LABELS = frozenset(
    {"charge", "chg", "ch", "charging", "cc", "cv", "ccc", "c"}
)


# --------------------------------------------------------------------------- #
# Header resolution
# --------------------------------------------------------------------------- #

_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
_BRACKET_RE = re.compile(r"[\(\[]\s*([A-Za-z%µ]+)\s*[\)\]]")


def norm_key(name: str) -> str:
    """Normalise a header into a comparison key: lowercase, alphanumerics only.

    ``"Discharge_Capacity (Ah)"`` -> ``"dischargecapacityah"``
    """
    return _NONALNUM_RE.sub("", (name or "").lower())


#: Normalised header -> canonical field.
#:
#: Deliberately omits the bare tokens ``t``, ``c``, ``h`` and ``i``: ``T`` is
#: temperature in some sources and time in others, and a silent wrong guess is
#: worse than an unmapped column the operator can fix with ``--map``. Bare ``v``
#: and ``i``-free voltage/current symbols that are unambiguous ARE included.
ALIASES: dict[str, str] = {
    # identity
    "cell": "cell_id",
    "cellid": "cell_id",
    "cellname": "cell_id",
    "cellno": "cell_id",
    "cellnumber": "cell_id",
    "barcode": "cell_id",
    "deviceid": "cell_id",
    "serial": "cell_id",
    "serialnumber": "cell_id",
    # cycling
    "cycle": "cycle_index",
    "cycleid": "cycle_index",
    "cycleindex": "cycle_index",
    "cyclenumber": "cycle_index",
    "cycleno": "cycle_index",
    "cyclenum": "cycle_index",
    "ncycle": "cycle_index",
    "cyclenr": "cycle_index",
    "step": "step_index",
    "stepindex": "step_index",
    "stepno": "step_index",
    "stepnumber": "step_index",
    "steptype": "step_type",
    "steplabel": "step_type",
    "stepname": "step_type",
    "mode": "step_type",
    "operation": "step_type",
    "action": "step_type",
    "command": "step_type",
    # time
    "time": "timestamp_s",
    "times": "timestamp_s",
    "timestamp": "timestamp_s",
    "testtime": "timestamp_s",
    "testtimes": "timestamp_s",
    "elapsed": "timestamp_s",
    "elapsedtime": "timestamp_s",
    "elapsedseconds": "timestamp_s",
    "datetime": "timestamp_s",
    # voltage
    "voltage": "voltage_v",
    "volts": "voltage_v",
    "volt": "voltage_v",
    "v": "voltage_v",
    "ecell": "voltage_v",
    "ecellvolt": "voltage_v",
    "cellvoltage": "voltage_v",
    "ewe": "voltage_v",
    "potential": "voltage_v",
    "ud": "voltage_v",
    # current
    "current": "current_a",
    "curr": "current_a",
    "amp": "current_a",
    "amps": "current_a",
    "ampere": "current_a",
    "amperage": "current_a",
    "iapp": "current_a",
    "i1": "current_a",
    # temperature
    "temperature": "temperature_c",
    "temp": "temperature_c",
    "temperaturec": "temperature_c",
    "tempc": "temperature_c",
    "tcell": "temperature_c",
    "celltemperature": "temperature_c",
    "temperaturedegc": "temperature_c",
    "temperaturekelvin": "temperature_c",
    "tempk": "temperature_c",
    "temperaturek": "temperature_c",
    "temperatur": "temperature_c",
    # capacity
    "chargecapacity": "charge_capacity_ah",
    "chargecapacityah": "charge_capacity_ah",
    "chargecap": "charge_capacity_ah",
    "chgcap": "charge_capacity_ah",
    "chgcapacity": "charge_capacity_ah",
    "ccharge": "charge_capacity_ah",
    "capacitycharge": "charge_capacity_ah",
    "dischargecapacity": "discharge_capacity_ah",
    "dischargecapacityah": "discharge_capacity_ah",
    "dischargecap": "discharge_capacity_ah",
    "dischgcap": "discharge_capacity_ah",
    "dchgcapacity": "discharge_capacity_ah",
    "cdischarge": "discharge_capacity_ah",
    "capacitydischarge": "discharge_capacity_ah",
}

#: ``(field, unit_token) -> (scale, offset)``. Affine because kelvin is not a
#: pure rescaling of celsius.
UNIT_SCALE: dict[tuple[str, str], tuple[float, float]] = {
    ("voltage_v", "v"): (1.0, 0.0),
    ("voltage_v", "mv"): (1e-3, 0.0),
    ("voltage_v", "uv"): (1e-6, 0.0),
    ("voltage_v", "volt"): (1.0, 0.0),
    ("voltage_v", "volts"): (1.0, 0.0),
    ("current_a", "a"): (1.0, 0.0),
    ("current_a", "ma"): (1e-3, 0.0),
    ("current_a", "ua"): (1e-6, 0.0),
    ("current_a", "amp"): (1.0, 0.0),
    ("current_a", "amps"): (1.0, 0.0),
    ("current_a", "ampere"): (1.0, 0.0),
    ("current_a", "amperes"): (1.0, 0.0),
    ("timestamp_s", "s"): (1.0, 0.0),
    ("timestamp_s", "sec"): (1.0, 0.0),
    ("timestamp_s", "secs"): (1.0, 0.0),
    ("timestamp_s", "seconds"): (1.0, 0.0),
    ("timestamp_s", "ms"): (1e-3, 0.0),
    ("timestamp_s", "min"): (60.0, 0.0),
    ("timestamp_s", "mins"): (60.0, 0.0),
    ("timestamp_s", "minutes"): (60.0, 0.0),
    ("timestamp_s", "hr"): (3600.0, 0.0),
    ("timestamp_s", "hrs"): (3600.0, 0.0),
    ("timestamp_s", "hour"): (3600.0, 0.0),
    ("timestamp_s", "hours"): (3600.0, 0.0),
    ("temperature_c", "c"): (1.0, 0.0),
    ("temperature_c", "degc"): (1.0, 0.0),
    ("temperature_c", "degreesc"): (1.0, 0.0),
    ("temperature_c", "celsius"): (1.0, 0.0),
    ("temperature_c", "k"): (1.0, -273.15),
    ("temperature_c", "kelvin"): (1.0, -273.15),
    ("charge_capacity_ah", "ah"): (1.0, 0.0),
    ("charge_capacity_ah", "mah"): (1e-3, 0.0),
    ("discharge_capacity_ah", "ah"): (1.0, 0.0),
    ("discharge_capacity_ah", "mah"): (1e-3, 0.0),
}

_token_fields: dict[str, set[str]] = {}
for _f, _t in UNIT_SCALE:
    _token_fields.setdefault(_t, set()).add(_f)

#: Longest first, so ``mah`` is tried before ``ah`` and ``seconds`` before ``s``.
PEEL_TOKENS: tuple[str, ...] = tuple(
    sorted(
        {
            "seconds", "minutes", "kelvin", "celcius", "celsius", "degreesc",
            "ampere", "amperes", "milliamp", "hours", "volts", "volt", "amps",
            "secs", "mins", "hrs", "degc", "mah", "sec", "min", "amp", "time",
            "hour", "ms", "mv", "ma", "uv", "ua", "ah", "hr", "v", "a", "k", "s",
        },
        key=len,
        reverse=True,
    )
)


def _raw_suffix(raw: str, n_alnum: int) -> str:
    """Trailing substring of ``raw`` containing its last ``n_alnum`` alphanumerics.

    Recovers the unit *as written* (``"mAh"``) rather than the normalised form
    (``"mah"``) that the matching is done on, so the inspect report shows the
    operator the same token they can see in their file.
    """
    if n_alnum <= 0:
        return ""
    out: list[str] = []
    seen = 0
    for ch in reversed(raw):
        out.append(ch)
        if ch.isalnum():
            seen += 1
            if seen == n_alnum:
                break
    return "".join(reversed(out)).strip()


def unit_transform(field_name: str, unit_token: str | None) -> tuple[float, float]:
    """Return ``(scale, offset)`` for a unit token on a given canonical field.

    Falls back to a cross-field lookup only when the token means exactly one
    thing across the whole table -- so ``mah`` resolves, but an ambiguous ``c``
    does not silently get applied.
    """
    tok = norm_key(unit_token or "")
    if not tok:
        return 1.0, 0.0
    direct = UNIT_SCALE.get((field_name, tok))
    if direct is not None:
        return direct
    owners = _token_fields.get(tok, set())
    if len(owners) == 1:
        return UNIT_SCALE[(next(iter(owners)), tok)]
    return 1.0, 0.0


def parse_header(header: str) -> tuple[str | None, str | None, str]:
    """Resolve one raw header cell.

    Returns ``(canonical_field | None, unit_token | None, how)`` where ``how``
    records how the match was made, for the ``inspect`` report.
    """
    raw = (header or "").strip()
    unit: str | None = None

    m = _BRACKET_RE.search(raw)
    if m:
        unit = m.group(1)
        raw = _BRACKET_RE.sub("", raw)

    key = norm_key(raw)
    if not key:
        return None, unit, "empty"

    if key in ALIASES:
        return ALIASES[key], unit, "exact"

    for tok in PEEL_TOKENS:
        if len(key) > len(tok) and key.endswith(tok):
            remainder = key[: -len(tok)]
            if remainder in ALIASES:
                # Report the unit as written in the header, not the normalised form.
                return ALIASES[remainder], (unit or _raw_suffix(raw, len(tok))), f"peeled:{tok}"

    return None, unit, "unmapped"


@dataclass
class ColumnMap:
    """Result of mapping a raw header row onto the canonical schema."""

    mapping: dict[str, str] = field(default_factory=dict)   # canonical -> raw header
    units: dict[str, str | None] = field(default_factory=dict)
    scales: dict[str, tuple[float, float]] = field(default_factory=dict)
    how: dict[str, str] = field(default_factory=dict)
    unscaled_units: list[str] = field(default_factory=list)
    targets: dict[str, str] = field(default_factory=dict)   # raw header -> canonical
    unmapped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def missing_required(self) -> list[str]:
        """Canonical fields the pipeline cannot work without."""
        return [f for f in ("voltage_v", "current_a", "timestamp_s") if f not in self.mapping]

    @property
    def resolved(self) -> bool:
        return not self.missing_required

    def scale_for(self, canonical: str) -> tuple[float, float]:
        return self.scales.get(canonical, (1.0, 0.0))

    def report(self) -> str:
        lines = ["column map (raw header -> canonical field, unit, transform)"]
        for canonical in CANONICAL_FIELDS:
            if canonical in self.mapping:
                scale, offset = self.scale_for(canonical)
                unit = self.units.get(canonical) or "-"
                lines.append(
                    f"  {self.mapping[canonical]:<28} -> {canonical:<22} "
                    f"unit={unit:<7} x{scale:g}{f' {offset:+g}' if offset else ''} "
                    f"({self.how.get(canonical, '')})"
                )
        for raw in self.conflicts:
            lines.append(f"  [conflict] {raw!r} also mapped to an already-filled field; first wins")
        for raw in self.unmapped:
            lines.append(f"  [unmapped] {raw!r} -- pass --map RAW=CANONICAL to bind it")
        if self.unscaled_units:
            lines.append(f"  [unit ignored, no known transform] {', '.join(self.unscaled_units)}")
        if self.missing_required:
            lines.append(f"  [REQUIRED MISSING] {', '.join(self.missing_required)}")
        return "\n".join(lines)


def resolve_columns(
    header: Sequence[str],
    overrides: dict[str, str] | None = None,
    field_units: dict[str, str] | None = None,
) -> ColumnMap:
    """Map a raw header row onto :data:`CANONICAL_FIELDS`.

    ``overrides`` binds an unresolved raw header explicitly (``{"T": "temperature_c"}``),
    which is how the ambiguous single-letter headers get resolved -- by the
    operator, visibly, rather than by a guess.
    """
    cmap = ColumnMap()
    overrides = {norm_key(k): v for k, v in (overrides or {}).items()}
    field_units = dict(field_units or {})

    for raw in header:
        if raw is None:
            continue
        canonical, unit, how = parse_header(raw)

        if how == "unmapped" and norm_key(raw) in overrides:
            canonical, how = overrides[norm_key(raw)], "override"
        elif canonical is not None and norm_key(raw) in overrides:
            canonical, how = overrides[norm_key(raw)], "override"

        if canonical is None:
            cmap.unmapped.append(raw)
            continue
        if canonical not in CANONICAL_FIELDS:
            cmap.unmapped.append(raw)
            continue
        if canonical in cmap.mapping and cmap.mapping[canonical] != raw:
            cmap.conflicts.append(raw)
            continue

        cmap.mapping[canonical] = raw
        cmap.targets[raw] = canonical
        cmap.how[canonical] = how
        cmap.units[canonical] = unit
        scale, offset = unit_transform(canonical, unit)
        if unit and (scale, offset) == (1.0, 0.0) and norm_key(unit) not in ("v", "a", "s", "c", "ah"):
            cmap.unscaled_units.append(f"{raw}:{unit}")
        cmap.scales[canonical] = (scale, offset)

        if canonical in field_units:
            forced = field_units[canonical]
            fscale, foffset = unit_transform(canonical, forced)
            cmap.scales[canonical] = (fscale, foffset)
            cmap.units[canonical] = forced

    return cmap


# --------------------------------------------------------------------------- #
# Reading messy files
# --------------------------------------------------------------------------- #

DELIMITERS = (",", ";", "\t", "|")


def detect_delimiter(sample: str) -> str:
    """Pick the delimiter that yields the best-scoring header row.

    Scores every candidate delimiter by how many of its split cells resolve to
    canonical fields -- more robust than counting separators, which preamble
    prose lines can out-vote.
    """
    best, best_score = ",", -1
    for delim in DELIMITERS:
        idx, hits = _best_header_line(sample.splitlines()[:40], delim)
        if hits > best_score:
            best, best_score = delim, hits
    return best


def _best_header_line(lines: Sequence[str], delim: str) -> tuple[int, int]:
    best_idx, best_hits = 0, -1
    for i, line in enumerate(lines):
        cells = line.split(delim)
        if len(cells) < 2:
            continue
        hits = sum(1 for c in cells if parse_header(c)[0] is not None)
        if hits > best_hits:
            best_idx, best_hits = i, hits
    return best_idx, best_hits


def find_header_row(lines: Sequence[str], delim: str) -> int:
    """Index of the real header row, skipping metadata preamble.

    Returns the line with the most resolvable cells; on a tie the earliest
    wins. Real field-data CSVs routinely carry 1-20 lines of instrument
    metadata before the data.
    """
    idx, _ = _best_header_line(lines, delim)
    return idx


def read_table(text: str) -> tuple[int, str, list[str], list[dict[str, str]]]:
    """Parse a messy CSV into ``(header_row_index, delimiter, header, rows)``."""
    delim = detect_delimiter(text)
    lines = text.splitlines()
    header_idx = find_header_row(lines, delim)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])), delimiter=delim)
    header = list(reader.fieldnames or [])
    rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
    return header_idx, delim, header, rows


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #

def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s or s.lower() in {"nan", "na", "n/a", "null", "none", "-", "..", ""}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_iso_seconds(value: Any) -> float | None:
    """Parse an ISO-ish datetime into epoch seconds, or ``None``."""
    s = str(value or "").strip()
    if not s:
        return None
    candidate = s.replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            return parser(candidate).timestamp()
        except (ValueError, TypeError):
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _label_family(value: Any) -> str | None:
    """Classify a step label into ``discharge`` / ``charge`` / ``rest``."""
    key = norm_key(str(value or ""))
    if not key:
        return None
    if key in DISCHARGE_LABELS:
        return "discharge"
    if key in CHARGE_LABELS:
        return "charge"
    if "discharge" in key or "dchg" in key:
        return "discharge"
    if "charge" in key or "chg" in key:
        return "charge"
    if "rest" in key or "pause" in key or "idle" in key or "ocv" in key:
        return "rest"
    return None


def normalize_records(
    rows: Iterable[dict[str, str]],
    cmap: ColumnMap,
    source_id: str = "",
    cell_id: str | None = None,
) -> list[dict[str, Any]]:
    """Coerce raw string rows into canonical-typed records (units applied)."""
    out: list[dict[str, Any]] = []
    for raw_row in rows:
        rec: dict[str, Any] = {f: None for f in CANONICAL_FIELDS}
        rec["source_id"] = source_id

        for canonical, raw_header in cmap.mapping.items():
            value = raw_row.get(raw_header)
            if canonical in NUMERIC_FIELDS:
                num = _to_float(value)
                if num is None and canonical == "timestamp_s":
                    num = _to_iso_seconds(value)
                scale, offset = cmap.scale_for(canonical)
                rec[canonical] = None if num is None else num * scale + offset
            elif canonical in INT_FIELDS:
                num = _to_float(value)
                rec[canonical] = None if num is None else int(num)
            else:
                text = str(value).strip() if value is not None else ""
                rec[canonical] = text or None

        # Traceability: keep the raw label alongside the derived family.
        if rec.get("step_type"):
            rec["step_family"] = _label_family(rec["step_type"])
        else:
            rec["step_family"] = None

        if cell_id:
            rec["cell_id"] = cell_id
        if not rec.get("cell_id"):
            rec["cell_id"] = "unknown"

        # Drop rows that carry no measurement at all (trailing blank lines,
        # footer notes, split comments).
        if all(rec.get(f) is None for f in NUMERIC_FIELDS):
            continue
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Sign convention
# --------------------------------------------------------------------------- #

@dataclass
class SignInference:
    convention: str
    reason: str
    confidence: str

    def report(self) -> str:
        return f"sign convention: {self.convention} ({self.confidence}) -- {self.reason}"


def infer_sign_convention(records: Sequence[dict[str, Any]]) -> SignInference:
    """Decide which direction of current means discharge.

    Canonical target: **positive current == discharge**.

    Where step labels exist they are authoritative -- that is a direct
    observation of intent. Without labels the sign distribution is used, which
    is weaker but usually decisive (a cycler that only ever logs discharge
    produces a one-sided distribution).
    """
    pairs = [
        (r.get("step_family"), r.get("current_a"))
        for r in records
        if r.get("current_a") is not None
    ]
    if not pairs:
        return SignInference("unsigned", "no current values present", "none")

    disc = [abs(i) * (1 if i > 0 else -1) for fam, i in pairs if fam == "discharge"]
    chg = [abs(i) * (1 if i > 0 else -1) for fam, i in pairs if fam == "charge"]

    if disc and chg and any(i != 0 for i in disc) and any(i != 0 for i in chg):
        md, mc = statistics.median(disc), statistics.median(chg)
        if md < 0 < mc:
            return SignInference(
                "negative_discharge",
                f"median discharge step current {md:.4g} A is negative while charge is {mc:.4g} A",
                "high",
            )
        if md > 0 > mc:
            return SignInference(
                "positive_discharge",
                f"median discharge step current {md:.4g} A is positive while charge is {mc:.4g} A",
                "high",
            )
        return SignInference(
            "unsigned",
            f"step labels present but currents do not oppose ({md:.4g} A vs {mc:.4g} A)",
            "low",
        )

    values = [i for _, i in pairs]
    has_neg = any(i < 0 for i in values)
    has_pos = any(i > 0 for i in values)
    if has_neg and not has_pos:
        return SignInference(
            "negative_discharge",
            "no step labels; all currents <= 0, so the source logs discharge as negative",
            "medium",
        )
    if has_pos and not has_neg:
        return SignInference(
            "positive_discharge",
            "no step labels; all currents >= 0 (magnitude-logging cycler)",
            "medium",
        )
    return SignInference(
        "unsigned",
        "no step labels and currents span both signs without a usable reference",
        "low",
    )


def apply_sign_convention(
    records: Sequence[dict[str, Any]], convention: str
) -> list[dict[str, Any]]:
    """Rewrite currents so positive == discharge. Returns new records."""
    if convention not in CONVENTIONS:
        raise ValueError(f"unknown convention {convention!r}; expected one of {CONVENTIONS}")
    if convention != "negative_discharge":
        return list(records)
    out = []
    for rec in records:
        rec = dict(rec)
        if rec.get("current_a") is not None:
            rec["current_a"] = -rec["current_a"]
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Time base
# --------------------------------------------------------------------------- #

def looks_like_epoch(values: Sequence[float]) -> bool:
    """True if timestamps look like absolute epoch seconds rather than elapsed."""
    clean = [v for v in values if v is not None]
    if len(clean) < 3:
        return False
    return statistics.median(clean) > 1e8


def to_elapsed_seconds(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert absolute timestamps to per-cell elapsed seconds.

    Only applied when the series actually looks absolute -- elapsed-second
    sources are already in canonical form and must not be shifted.
    """
    by_cell: dict[str, list[float]] = {}
    for rec in records:
        if rec.get("timestamp_s") is not None:
            by_cell.setdefault(rec["cell_id"], []).append(rec["timestamp_s"])

    offsets: dict[str, float] = {}
    for cell, values in by_cell.items():
        if looks_like_epoch(values):
            offsets[cell] = min(values)

    if not offsets:
        return list(records)

    out = []
    for rec in records:
        rec = dict(rec)
        base = offsets.get(rec["cell_id"])
        if base is not None and rec.get("timestamp_s") is not None:
            rec["timestamp_s"] = rec["timestamp_s"] - base
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Coulomb counting
# --------------------------------------------------------------------------- #

def integrate_capacity(
    points: Sequence[tuple[float, float]],
    discharge_positive: bool = True,
) -> float:
    """Trapezoidal Coulomb counting over ``(t_seconds, current_a)`` points.

    Returns ampere-hours. Exact for a constant current (the trapezoid between
    equal samples averages to that current), which is the sanity property the
    test suite pins.

    Sign: with ``discharge_positive`` the magnitude is accumulated, i.e. this is
    *charge throughput*, not net charge. Net charge accumulates signed current.
    """
    clean = [(t, i) for t, i in points if t is not None and i is not None]
    if len(clean) < 2:
        return 0.0
    clean.sort(key=lambda p: p[0])
    total = 0.0
    for (t0, i0), (t1, i1) in zip(clean, clean[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        a, b = (abs(i0), abs(i1)) if discharge_positive else (i0, i1)
        total += 0.5 * (a + b) * dt
    return total / 3600.0


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_record(rec: dict[str, Any], strict_voltage: bool = False) -> list[str]:
    """Range and consistency checks for one canonical record."""
    issues: list[str] = []
    for fname, (lo, hi) in REALISTIC_RANGES.items():
        value = rec.get(fname)
        if value is None:
            continue
        if not (lo <= value <= hi):
            issues.append(f"{fname}={value!r} outside plausible range [{lo}, {hi}]")
    v = rec.get("voltage_v")
    if strict_voltage and v is not None and v <= 0:
        issues.append(f"voltage_v={v!r} is non-positive; a live cell cannot measure <= 0 V")
    if rec.get("cycle_index") is not None and rec["cycle_index"] < 0:
        issues.append(f"cycle_index={rec['cycle_index']!r} is negative")
    return issues


def validate_series(records: Sequence[dict[str, Any]]) -> list[str]:
    """Cross-record checks: monotonic time per cell, one step family per cycle."""
    issues: list[str] = []
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_cell.setdefault(str(rec.get("cell_id")), []).append(rec)

    for cell, recs in by_cell.items():
        stamps = [r["timestamp_s"] for r in recs if r.get("timestamp_s") is not None]
        drops = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
        if drops:
            issues.append(f"cell {cell}: timestamp decreases {drops} time(s); check time base/units")

        steps = {r.get("step_index") for r in recs if r.get("step_index") is not None}
        if steps and len(steps) > 1:
            fams = {r.get("step_family") for r in recs if r.get("step_family")}
            missing = [s for s in steps if not any(
                r.get("step_index") == s and r.get("step_family") for r in recs
            )]
            if missing and fams:
                issues.append(f"cell {cell}: step indices {sorted(missing)[:5]} have no label family")
    return issues


# --------------------------------------------------------------------------- #
# Summary + writing
# --------------------------------------------------------------------------- #

def summarise(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Cheap profile of a normalised batch, for logs and the inspect report."""
    cells = {r.get("cell_id") for r in records}
    cycles = {r.get("cycle_index") for r in records if r.get("cycle_index") is not None}
    volts = [r["voltage_v"] for r in records if r.get("voltage_v") is not None]
    temps = [r["temperature_c"] for r in records if r.get("temperature_c") is not None]
    fams: dict[str, int] = {}
    for r in records:
        fam = r.get("step_family") or "unlabelled"
        fams[fam] = fams.get(fam, 0) + 1

    def span(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {"min": min(values), "max": max(values), "mean": statistics.fmean(values)}

    return {
        "records": len(records),
        "cells": len(cells),
        "cycles": len(cycles),
        "step_families": fams,
        "voltage_v": span(volts),
        "temperature_c": span(temps),
    }


def write_records(records: Sequence[dict[str, Any]], path: str) -> str:
    """Write canonical records to ``.parquet`` (needs duckdb) or ``.csv``.

    Returns the path written. Parquet goes through duckdb rather than pyarrow so
    the container does not need a heavy Arrow build.
    """
    if not records:
        raise ValueError("refusing to write an empty record set")

    # Union of keys, canonical order first, extras after.
    extra = sorted({k for r in records for k in r} - set(CANONICAL_FIELDS) - {"step_family"})
    cols = list(CANONICAL_FIELDS) + (["step_family"] if "step_family" in records[0] else []) + extra

    if path.endswith(".parquet"):
        try:
            import duckdb  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Parquet output needs duckdb (pip install duckdb); "
                "or write .csv and convert in the container"
            ) from exc

        import tempfile
        import os

        fd, tmp_csv = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _write_csv(records, tmp_csv, cols)
            con = duckdb.connect()
            try:
                # all_varchar=true keeps the read faithful, then the numeric and
                # integer columns are cast explicitly. Two reasons this is not
                # left to inference: a column that happens to be all-null in this
                # batch would otherwise be typed from no evidence, and integers
                # read as VARCHAR would hand strings to every downstream consumer.
                double_casts = ", ".join(
                    f'TRY_CAST("{c}" AS DOUBLE) AS "{c}"' for c in cols if c in NUMERIC_FIELDS
                )
                int_casts = ", ".join(
                    f'TRY_CAST("{c}" AS BIGINT) AS "{c}"' for c in cols if c in INT_FIELDS
                )
                passthrough = ", ".join(
                    f'"{c}"' for c in cols if c not in NUMERIC_FIELDS and c not in INT_FIELDS
                )
                projection = ", ".join(p for p in (double_casts, int_casts, passthrough) if p)
                con.execute(
                    "COPY (SELECT {proj} FROM read_csv('{src}', header=true, all_varchar=true)) "
                    "TO '{dst}' (FORMAT PARQUET)".format(
                        proj=projection,
                        src=tmp_csv.replace("\\", "/"),
                        dst=path.replace("\\", "/"),
                    )
                )
            finally:
                con.close()
        finally:
            try:
                os.unlink(tmp_csv)
            except OSError:
                pass
        return path

    _write_csv(records, path, cols)
    return path


def _write_csv(records: Sequence[dict[str, Any]], path: str, cols: Sequence[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(cols), extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow({c: rec.get(c) for c in cols})
