"""Command line interface: ``list`` | ``inspect`` | ``normalize`` | ``status``.

``inspect`` is the important one operationally. Before a release is normalised it
prints exactly what the pipeline decided -- which raw header became which
canonical field, what unit transform was applied, what sign convention was
inferred and on what evidence. Silent wrong guesses are the main failure mode of
heterogeneous ingestion, so the decision is always shown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, fetch, normalize, registry, state as state_mod

DEFAULT_STATE = "out/state.json"


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _parse_pairs(items: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--{what} expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def cmd_list(args: argparse.Namespace) -> int:
    releases = registry.filter_releases(
        kind=args.kind, redistributable_only=args.redistributable_only, tag=args.tag
    )
    print(registry.render_table(releases))
    print()
    print(f"{len(releases)} of {len(registry.RELEASES)} releases shown")
    if args.policy:
        print()
        print(registry.DISTRIBUTION_POLICY)
    return 0


def _load(args: argparse.Namespace):
    text = _read_text(args.path)
    header_idx, delim, header, rows = normalize.read_table(text)
    overrides = _parse_pairs(args.map, "map")
    field_units = _parse_pairs(args.unit, "unit")
    cmap = normalize.resolve_columns(header, overrides=overrides, field_units=field_units)
    return text, header_idx, delim, header, rows, cmap


def cmd_inspect(args: argparse.Namespace) -> int:
    text, header_idx, delim, header, rows, cmap = _load(args)
    delim_name = {"\t": "\\t"}.get(delim, delim)

    print(f"file        : {args.path}")
    print(f"size        : {len(text):,} chars, {len(rows):,} data rows")
    print(f"delimiter   : {delim_name!r}")
    print(f"header row  : index {header_idx} (0-based); "
          f"{header_idx} preamble line(s) skipped")
    print(f"columns     : {len(header)} raw / {len(cmap.mapping)} mapped")
    print()
    print(cmap.report())

    if not cmap.resolved or args.no_data:
        if not cmap.resolved:
            print()
            print("cannot inspect values: required columns unresolved (see above)")
            return 2
        return 0

    records = normalize.normalize_records(rows, cmap, source_id=args.source_id or "inspect")
    if args.limit:
        records = records[: args.limit]

    inference = normalize.infer_sign_convention(records)
    print()
    print(inference.report())

    if args.apply_sign:
        records = normalize.apply_sign_convention(records, inference.convention)
        print("(sign convention applied -> positive current == discharge)")

    used_elapsed = False
    if not args.no_elapsed:
        before = [r["timestamp_s"] for r in records[:5] if r.get("timestamp_s") is not None]
        records = normalize.to_elapsed_seconds(records)
        after = [r["timestamp_s"] for r in records[:5] if r.get("timestamp_s") is not None]
        used_elapsed = before != after
        if used_elapsed:
            print("(absolute timestamps detected -> converted to per-cell elapsed seconds)")

    stats = normalize.summarise(records)
    print()
    print("summary")
    print(f"  records          : {stats['records']:,}")
    print(f"  cells            : {stats['cells']}")
    print(f"  cycles           : {stats['cycles']}")
    step_bits = ", ".join(f"{k}={v:,}" for k, v in sorted(stats["step_families"].items()))
    print(f"  step families    : {step_bits}")
    for field in ("voltage_v", "temperature_c"):
        span = stats[field]
        if span:
            print(f"  {field:<17}: min={span['min']:.4g} max={span['max']:.4g} mean={span['mean']:.4g}")

    issues: list[str] = []
    for i, rec in enumerate(records):
        for issue in normalize.validate_record(rec):
            issues.append(f"row {i}: {issue}")
            if len(issues) >= 10:
                break
        if len(issues) >= 10:
            break
    series_issues = normalize.validate_series(records)

    print()
    if issues or series_issues:
        print(f"validation: {len(issues) + len(series_issues)} issue(s) in the first batch")
        for issue in (issues + series_issues)[:10]:
            print(f"  - {issue}")
    else:
        print("validation: clean")
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    text, header_idx, delim, header, rows, cmap = _load(args)

    if not cmap.resolved:
        print(f"file        : {args.path}")
        print(f"delimiter   : {delim!r}, header row {header_idx}, {len(rows):,} rows")
        print()
        print(cmap.report())
        print()
        print("refusing to normalise: required columns unresolved")
        return 2

    records = normalize.normalize_records(
        rows, cmap, source_id=args.source_id or "", cell_id=args.cell_id
    )
    if not records:
        print("no records parsed -- every row was empty after coercion")
        return 1

    if args.sign == "auto":
        inference = normalize.infer_sign_convention(records)
        print(inference.report())
        convention = inference.convention
    else:
        convention = args.sign
        print(f"sign convention: {convention} (forced)")

    if convention != "unsigned":
        records = normalize.apply_sign_convention(records, convention)

    shifted = False
    if not args.no_elapsed:
        before = [r["timestamp_s"] for r in records if r.get("timestamp_s") is not None][:5]
        records = normalize.to_elapsed_seconds(records)
        after = [r["timestamp_s"] for r in records if r.get("timestamp_s") is not None][:5]
        shifted = before != after
        if shifted:
            print("time base: absolute timestamps converted to elapsed seconds")

    if args.source_id:
        allowed, why = True, ""
        try:
            allowed, why = fetch.can_publish_raw(args.source_id)
        except KeyError:
            why = f"source {args.source_id!r} is not in the catalogue"
        if not allowed and args.public:
            print(f"REFUSING --public write: {why}")
            return 3

    written = normalize.write_records(records, args.output)
    issues = [
        f"row {i}: {msg}"
        for i, rec in enumerate(records)
        for msg in normalize.validate_record(rec)
    ]
    stats = normalize.summarise(records)

    print()
    print(f"rows in      : {len(rows):,}")
    print(f"rows out     : {len(records):,}")
    print(f"cells        : {stats['cells']}   cycles: {stats['cycles']}")
    print(f"validation   : {len(issues)} issue(s)")
    for issue in issues[:5]:
        print(f"  - {issue}")
    if len(issues) > 5:
        print(f"  ... {len(issues) - 5} more")
    print(f"written      : {written}")

    if args.state:
        st = state_mod.load(args.state)
        st.job = st.job if st.job != "unnamed" else "normalize"
        st.finish(os.path.basename(args.path), rows_out=len(records), output=written)
        state_mod.save(st, args.state)
        print(f"state        : {args.state} ({st.progress()})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not os.path.exists(args.state):
        print(f"no state file at {args.state}")
        return 1
    st = state_mod.load(args.state)
    print(f"job          : {st.job}")
    print(f"progress     : {st.progress()}")
    print(f"complete     : {st.is_complete()}")
    print(f"updated      : {st.updated_at}")
    if st.meta:
        print(f"meta         : {json.dumps(st.meta, sort_keys=True)}")
    if st.units:
        print()
        for unit in st.units.values():
            extra = f" rows={unit.rows_out}" if unit.rows_out is not None else ""
            err = f" error={unit.error[:60]!r}" if unit.error else ""
            print(f"  {unit.unit_id:<32} {unit.status:<8} attempts={unit.attempts}{extra}{err}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bfa",
        description="Ingest, normalise and diagnose public battery field data.",
    )
    parser.add_argument("--version", action="version", version=f"battery-field-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show the release catalogue")
    p_list.add_argument("--kind", choices=["lab", "field"])
    p_list.add_argument("--tag")
    p_list.add_argument("--redistributable-only", action="store_true",
                        help="only releases whose rows may be republished")
    p_list.add_argument("--policy", action="store_true", help="also print the distribution policy")
    p_list.set_defaults(func=cmd_list)

    def add_mapping_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--map", action="append", metavar="RAW=CANONICAL",
                       help="bind an unresolved raw header (repeatable), e.g. --map I=current_a")
        p.add_argument("--unit", action="append", metavar="FIELD=UNIT",
                       help="force a field's unit when the header carries none, e.g. --unit temperature_c=kelvin")
        p.add_argument("--source-id", help="release id from the catalogue (enables licence checks)")
        p.add_argument("--no-elapsed", action="store_true",
                       help="keep the time base as-is instead of converting absolute stamps to elapsed seconds")
        p.add_argument("--limit", type=int, help="limit rows considered (inspect only)")

    p_inspect = sub.add_parser("inspect", help="report the mapping decisions for a file")
    p_inspect.add_argument("path")
    p_inspect.add_argument("--apply-sign", action="store_true", help="apply the inferred sign convention")
    p_inspect.add_argument("--no-data", action="store_true", help="only report the column map")
    add_mapping_flags(p_inspect)
    p_inspect.set_defaults(func=cmd_inspect, cell_id=None)

    p_norm = sub.add_parser("normalize", help="write canonical records")
    p_norm.add_argument("path")
    p_norm.add_argument("-o", "--output", required=True, help="output .parquet or .csv")
    p_norm.add_argument("--cell-id", help="override the cell id for every row")
    p_norm.add_argument("--sign", choices=["auto", *normalize.CONVENTIONS], default="auto")
    p_norm.add_argument("--state", help="job state file to update (resumable)")
    p_norm.add_argument("--public", action="store_true",
                        help="fail if the source release's rows may not be republished")
    add_mapping_flags(p_norm)
    p_norm.set_defaults(func=cmd_normalize)

    p_status = sub.add_parser("status", help="show job state")
    p_status.add_argument("--state", default=DEFAULT_STATE)
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
