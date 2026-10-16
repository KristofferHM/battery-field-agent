"""Test suite for the canonical ingestion core.

Stdlib ``unittest`` on purpose: the pipeline runs on a laptop, in a container and
in a Kaggle kernel, and the test suite must be runnable in all three without
installing a test framework first.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from battery_field_agent import fetch, normalize, registry, state as state_mod  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8-sig") as fh:
        return fh.read()


def load(name: str):
    """Parse a fixture into (header_idx, delimiter, header, rows, column_map)."""
    header_idx, delim, header, rows = normalize.read_table(fixture(name))
    return header_idx, delim, header, rows, normalize.resolve_columns(header)


# --------------------------------------------------------------------------- #
# Header resolution
# --------------------------------------------------------------------------- #

class TestHeaderParsing(unittest.TestCase):
    def test_norm_key_strips_punctuation_and_case(self):
        self.assertEqual(normalize.norm_key("Discharge_Capacity (Ah)"), "dischargecapacityah")
        self.assertEqual(normalize.norm_key("  Ecell  "), "ecell")
        self.assertEqual(normalize.norm_key(""), "")

    def test_exact_aliases(self):
        for raw, expected in [
            ("Cell", "cell_id"),
            ("Cycle", "cycle_index"),
            ("StepType", "step_type"),
            ("Ecell", "voltage_v"),
            ("barcode", "cell_id"),
            ("chg_cap", "charge_capacity_ah"),
            ("dischg_cap", "discharge_capacity_ah"),
        ]:
            self.assertEqual(normalize.parse_header(raw)[0], expected, msg=raw)

    def test_bracketed_unit_is_extracted_not_matched(self):
        canonical, unit, how = normalize.parse_header("Voltage(mV)")
        self.assertEqual((canonical, unit, how), ("voltage_v", "mV", "exact"))
        canonical, unit, _ = normalize.parse_header("Time(ms)")
        self.assertEqual((canonical, unit), ("timestamp_s", "ms"))
        canonical, unit, _ = normalize.parse_header("Discharge_Capacity(mAh)")
        self.assertEqual((canonical, unit), ("discharge_capacity_ah", "mAh"))

    def test_unit_suffix_is_peeled_from_the_name(self):
        canonical, unit, how = normalize.parse_header("Voltage_mV")
        self.assertEqual((canonical, unit), ("voltage_v", "mV"))
        self.assertTrue(how.startswith("peeled:"), how)
        self.assertEqual(normalize.parse_header("current_mA")[0], "current_a")
        self.assertEqual(normalize.parse_header("Time_ms")[0], "timestamp_s")
        self.assertEqual(normalize.parse_header("temp_c")[0], "temperature_c")

    def test_peeling_prefers_the_longest_token(self):
        # "mah" must win over "ah", otherwise a mAh column is read as Ah -- a
        # silent 1000x error.
        canonical, unit, _ = normalize.parse_header("chg_cap_mAh")
        self.assertEqual((canonical, unit), ("charge_capacity_ah", "mAh"))

    def test_ambiguous_single_letters_are_left_unmapped(self):
        # T/t and I/i collide across temperature/time and current/index, and a
        # silent wrong guess corrupts the dataset. These must surface as
        # unmapped so the operator can bind them with --map.
        for raw in ("I", "i", "T", "t", "c"):
            canonical, _, how = normalize.parse_header(raw)
            self.assertIsNone(canonical, msg=f"{raw!r} should be unmapped, got {canonical!r}")
            self.assertEqual(how, "unmapped", msg=raw)

    def test_empty_header(self):
        self.assertEqual(normalize.parse_header("")[2], "empty")
        self.assertEqual(normalize.parse_header(None)[2], "empty")


class TestUnitTransform(unittest.TestCase):
    def test_direct_lookups(self):
        self.assertEqual(normalize.unit_transform("voltage_v", "mV"), (1e-3, 0.0))
        self.assertEqual(normalize.unit_transform("timestamp_s", "ms"), (1e-3, 0.0))
        self.assertEqual(normalize.unit_transform("timestamp_s", "min"), (60.0, 0.0))
        self.assertEqual(normalize.unit_transform("charge_capacity_ah", "mAh"), (1e-3, 0.0))
        self.assertEqual(normalize.unit_transform("current_a", "A"), (1.0, 0.0))

    def test_kelvin_is_affine_not_scalar(self):
        self.assertEqual(normalize.unit_transform("temperature_c", "kelvin"), (1.0, -273.15))

    def test_missing_unit_is_identity(self):
        self.assertEqual(normalize.unit_transform("voltage_v", None), (1.0, 0.0))
        self.assertEqual(normalize.unit_transform("voltage_v", ""), (1.0, 0.0))

    def test_cross_field_fallback_only_when_unambiguous(self):
        # "kelvin" means exactly one thing, so it resolves even against a field
        # mismatch. "ah" belongs to two capacity fields, so it must NOT resolve.
        self.assertEqual(normalize.unit_transform("voltage_v", "kelvin"), (1.0, -273.15))
        self.assertEqual(normalize.unit_transform("voltage_v", "Ah"), (1.0, 0.0))


class TestColumnResolution(unittest.TestCase):
    def test_conflicts_keep_the_first_binding(self):
        cmap = normalize.resolve_columns(["V", "voltage", "I"])
        self.assertEqual(cmap.mapping["voltage_v"], "V")
        self.assertIn("voltage", cmap.conflicts)

    def test_overrides_bind_an_unresolved_header(self):
        cmap = normalize.resolve_columns(
            ["Ecell", "I", "test_time"], overrides={"I": "current_a"}
        )
        self.assertEqual(cmap.mapping["current_a"], "I")
        self.assertEqual(cmap.how["current_a"], "override")
        self.assertTrue(cmap.resolved)

    def test_an_override_cannot_rescue_a_missing_required_field(self):
        cmap = normalize.resolve_columns(["Ecell", "I"], overrides={"I": "current_a"})
        self.assertEqual(cmap.mapping["current_a"], "I")
        self.assertCountEqual(cmap.missing_required, ["timestamp_s"])

    def test_required_fields_are_reported_when_missing(self):
        cmap = normalize.resolve_columns(["Cycle", "Temperature"])
        self.assertFalse(cmap.resolved)
        self.assertCountEqual(cmap.missing_required, ["voltage_v", "current_a", "timestamp_s"])


# --------------------------------------------------------------------------- #
# File structure detection
# --------------------------------------------------------------------------- #

class TestStructureDetection(unittest.TestCase):
    def test_comma_delimiter_and_preamble_skip(self):
        header_idx, delim, header, rows = normalize.read_table(fixture("messy_mv_ma.csv"))
        self.assertEqual(delim, ",")
        self.assertEqual(header_idx, 6, "6 preamble lines precede the real header")
        self.assertEqual(len(rows), 6)
        self.assertEqual(header[0], "Cell")

    def test_semicolon_delimiter(self):
        _, delim, header, rows = normalize.read_table(fixture("alias_semicolon_kelvin.csv"))
        self.assertEqual(delim, ";")
        self.assertEqual(len(header), 8)
        self.assertEqual(len(rows), 6)

    def test_detect_delimiter_ignores_prose_preamble(self):
        sample = "a note, with commas\nand, more, commas\nTime,Voltage,Current\n1,2,3\n"
        # prose lines contain more commas than the header, so counting would be
        # fooled; resolvability scoring is not.
        self.assertEqual(normalize.detect_delimiter(sample), ",")


# --------------------------------------------------------------------------- #
# End-to-end normalisation
# --------------------------------------------------------------------------- #

class TestEndToEndMessyMvMa(unittest.TestCase):
    """Messy fixture 1: mV/mA/ms units, negative-discharge source, preamble."""

    def setUp(self):
        _, _, header, rows, cmap = load("messy_mv_ma.csv")
        self.cmap = cmap
        self.rows = rows
        records = normalize.normalize_records(rows, cmap, source_id="nasa_pcoe_randomized")
        self.inference = normalize.infer_sign_convention(records)
        self.records = normalize.apply_sign_convention(records, self.inference.convention)

    def test_every_column_mapped(self):
        self.assertTrue(self.cmap.resolved)
        self.assertCountEqual(self.cmap.unmapped, [])
        self.assertEqual(len(self.cmap.mapping), 10)

    def test_sign_convention_detected_from_step_labels(self):
        self.assertEqual(self.inference.convention, "negative_discharge")
        self.assertEqual(self.inference.confidence, "high")

    def test_units_are_reconciled(self):
        first = self.records[0]
        self.assertAlmostEqual(first["voltage_v"], 3.2)          # 3200 mV
        self.assertAlmostEqual(first["timestamp_s"], 0.0)        # 0 ms
        self.assertAlmostEqual(first["current_a"], -1.5)         # 1500 mA charge -> negative
        self.assertAlmostEqual(first["temperature_c"], 24.1)     # C passthrough
        self.assertAlmostEqual(first["charge_capacity_ah"], 0.0)
        self.assertAlmostEqual(self.records[1]["timestamp_s"], 60.0)   # 60000 ms
        self.assertAlmostEqual(self.records[2]["charge_capacity_ah"], 0.05)  # 50 mAh

    def test_canonical_sign_is_positive_discharge(self):
        for rec in self.records:
            if rec["step_family"] == "discharge":
                self.assertGreater(rec["current_a"], 0, "discharge must end up positive")
            elif rec["step_family"] == "charge":
                self.assertLess(rec["current_a"], 0, "charge must end up negative")

    def test_reported_capacity_matches_coulomb_counting(self):
        """The independent cross-check: integrate the current and compare with
        the capacity column the instrument reported. If unit reconciliation or
        the sign flip were wrong, these two numbers would not agree."""
        discharge = [
            (r["timestamp_s"], r["current_a"])
            for r in self.records
            if r["step_family"] == "discharge"
        ]
        integrated = normalize.integrate_capacity(discharge)
        reported = max(
            r["discharge_capacity_ah"] for r in self.records
            if r["discharge_capacity_ah"] is not None
        )
        self.assertAlmostEqual(integrated, reported, places=6)
        self.assertAlmostEqual(integrated, 0.05, places=6)

    def test_validation_is_clean(self):
        for rec in self.records:
            self.assertEqual(normalize.validate_record(rec), [])
        self.assertEqual(normalize.validate_series(self.records), [])

    def test_summary(self):
        stats = normalize.summarise(self.records)
        self.assertEqual(stats["records"], 6)
        self.assertEqual(stats["cells"], 1)
        self.assertEqual(stats["cycles"], 1)
        self.assertEqual(stats["step_families"], {"charge": 3, "discharge": 3})


class TestEndToEndAliasSemicolonKelvin(unittest.TestCase):
    """Messy fixture 2: semicolon, alias names, kelvin, magnitude-logged current."""

    def setUp(self):
        _, _, header, rows, _ = load("alias_semicolon_kelvin.csv")
        cmap = normalize.resolve_columns(
            header,
            overrides={"I": "current_a"},
            field_units={"temperature_c": "kelvin"},
        )
        self.cmap = cmap
        self.records = normalize.normalize_records(rows, cmap, source_id="oxford_obdd1")

    def test_aliases_resolve(self):
        self.assertEqual(self.cmap.mapping["cell_id"], "barcode")
        self.assertEqual(self.cmap.mapping["cycle_index"], "cycleNo")
        self.assertEqual(self.cmap.mapping["timestamp_s"], "test_time")
        self.assertEqual(self.cmap.mapping["voltage_v"], "Ecell")
        self.assertTrue(self.cmap.resolved)

    def test_override_binds_the_ambiguous_current_column(self):
        self.assertEqual(self.cmap.mapping["current_a"], "I")
        self.assertEqual(self.cmap.how["current_a"], "override")

    def test_kelvin_converted_to_celsius(self):
        self.assertAlmostEqual(self.records[0]["temperature_c"], 24.0, places=6)   # 297.15 K
        self.assertAlmostEqual(self.records[2]["temperature_c"], 24.5, places=6)   # 297.65 K
        self.assertAlmostEqual(self.records[5]["temperature_c"], 25.3, places=6)   # 298.45 K

    def test_magnitude_logged_current_infers_positive_discharge(self):
        inference = normalize.infer_sign_convention(self.records)
        self.assertEqual(inference.convention, "positive_discharge")
        self.assertEqual(inference.confidence, "medium")


class TestEndToEndIsoTimestamps(unittest.TestCase):
    """Messy fixture 3: ISO datetimes, already-canonical sign and units."""

    def setUp(self):
        _, _, header, rows, cmap = load("iso_datetime_positive_discharge.csv")
        self.cmap = cmap
        records = normalize.normalize_records(rows, cmap, source_id="calce_cs2")
        self.inference = normalize.infer_sign_convention(records)
        self.records = normalize.to_elapsed_seconds(
            normalize.apply_sign_convention(records, self.inference.convention)
        )

    def test_unit_suffixes_peeled(self):
        self.assertEqual(self.cmap.mapping["voltage_v"], "voltage_v")
        self.assertEqual(self.cmap.mapping["current_a"], "current_a")
        self.assertEqual(self.cmap.mapping["step_type"], "step_type")

    def test_positive_discharge_detected_and_left_alone(self):
        self.assertEqual(self.inference.convention, "positive_discharge")
        self.assertEqual(self.inference.confidence, "high")
        self.assertGreater(self.records[2]["current_a"], 0)   # discharge row
        self.assertLess(self.records[0]["current_a"], 0)      # charge row

    def test_iso_datetimes_become_elapsed_seconds(self):
        self.assertAlmostEqual(self.records[0]["timestamp_s"], 0.0)
        self.assertAlmostEqual(self.records[1]["timestamp_s"], 60.0)
        self.assertAlmostEqual(self.records[3]["timestamp_s"], 180.0)


class TestTimeBase(unittest.TestCase):
    def test_epoch_detection(self):
        self.assertTrue(normalize.looks_like_epoch([1.77e9, 1.77e9 + 60, 1.77e9 + 120]))
        self.assertFalse(normalize.looks_like_epoch([0.0, 60.0, 120.0]))
        self.assertFalse(normalize.looks_like_epoch([1.0, 2.0]))

    def test_elapsed_conversion_is_per_cell(self):
        records = [
            {"cell_id": "a", "timestamp_s": 1_770_000_000.0},
            {"cell_id": "a", "timestamp_s": 1_770_000_060.0},
            {"cell_id": "a", "timestamp_s": 1_770_000_120.0},
            {"cell_id": "b", "timestamp_s": 1_770_000_500.0},
            {"cell_id": "b", "timestamp_s": 1_770_000_560.0},
            {"cell_id": "b", "timestamp_s": 1_770_000_620.0},
        ]
        out = normalize.to_elapsed_seconds(records)
        self.assertAlmostEqual(out[0]["timestamp_s"], 0.0)
        self.assertAlmostEqual(out[3]["timestamp_s"], 0.0)
        self.assertAlmostEqual(out[5]["timestamp_s"], 120.0)

    def test_elapsed_input_is_not_shifted(self):
        records = [
            {"cell_id": "a", "timestamp_s": 0.0},
            {"cell_id": "a", "timestamp_s": 60.0},
            {"cell_id": "a", "timestamp_s": 120.0},
        ]
        out = normalize.to_elapsed_seconds(records)
        self.assertEqual([r["timestamp_s"] for r in out], [0.0, 60.0, 120.0])


# --------------------------------------------------------------------------- #
# Coulomb counting
# --------------------------------------------------------------------------- #

class TestCoulombCounting(unittest.TestCase):
    def test_constant_current_is_exact(self):
        # 1 A for one hour, sampled every 60 s -> exactly 1 Ah.
        points = [(60.0 * i, 1.0) for i in range(61)]
        self.assertAlmostEqual(normalize.integrate_capacity(points), 1.0, places=9)

    def test_magnitude_is_accumulated(self):
        """Charge throughput uses |I|, so a symmetric charge/discharge cycle
        accumulates throughput rather than cancelling to zero."""
        up = [(60.0 * i, 1.0) for i in range(31)]
        down = [(60.0 * (i + 30), -1.0) for i in range(31)]
        self.assertAlmostEqual(normalize.integrate_capacity(up + down), 1.0, places=9)

    def test_signed_mode_cancels(self):
        up = [(60.0 * i, 1.0) for i in range(31)]
        down = [(60.0 * (i + 30), -1.0) for i in range(31)]
        self.assertAlmostEqual(
            normalize.integrate_capacity(up + down, discharge_positive=False), 0.0, places=9
        )

    def test_trapezoid_not_rectangle(self):
        # 0 A then 2 A over one hour -> trapezoid gives 1 Ah, rectangle would give 2.
        self.assertAlmostEqual(normalize.integrate_capacity([(0.0, 0.0), (3600.0, 2.0)]), 1.0, places=9)

    def test_unsorted_input_is_sorted(self):
        points = [(120.0, 1.0), (0.0, 1.0), (60.0, 1.0)]
        self.assertAlmostEqual(normalize.integrate_capacity(points), 120.0 / 3600.0, places=9)

    def test_degenerate_inputs(self):
        self.assertEqual(normalize.integrate_capacity([]), 0.0)
        self.assertEqual(normalize.integrate_capacity([(0.0, 1.0)]), 0.0)
        self.assertEqual(normalize.integrate_capacity([(0.0, None), (60.0, None)]), 0.0)

    def test_duplicate_timestamps_do_not_contribute(self):
        points = [(0.0, 1.0), (0.0, 1.0), (60.0, 1.0)]
        self.assertAlmostEqual(normalize.integrate_capacity(points), 60.0 / 3600.0, places=9)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

class TestValidation(unittest.TestCase):
    def test_range_violations_are_caught(self):
        issues = normalize.validate_record({"voltage_v": 3200.0, "temperature_c": 24.0})
        self.assertTrue(any("voltage_v" in i for i in issues), issues)
        # 3200 mV read as volts is exactly the unit bug this catches.

    def test_plausible_record_is_clean(self):
        rec = {
            "voltage_v": 3.7, "current_a": 1.5, "temperature_c": 25.0,
            "cycle_index": 3, "charge_capacity_ah": 2.5,
        }
        self.assertEqual(normalize.validate_record(rec), [])

    def test_negative_cycle_index_flagged(self):
        issues = normalize.validate_record({"cycle_index": -1})
        self.assertTrue(any("cycle_index" in i for i in issues))

    def test_none_values_are_not_flagged(self):
        self.assertEqual(normalize.validate_record({"voltage_v": None}), [])

    def test_non_monotonic_time_flagged(self):
        records = [
            {"cell_id": "a", "timestamp_s": 100.0},
            {"cell_id": "a", "timestamp_s": 50.0},
        ]
        issues = normalize.validate_series(records)
        self.assertTrue(any("decreases" in i for i in issues), issues)


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #

class TestWriters(unittest.TestCase):
    def setUp(self):
        _, _, _, rows, cmap = load("messy_mv_ma.csv")
        self.records = normalize.normalize_records(rows, cmap, source_id="nasa_pcoe_randomized")

    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            normalize.write_records(self.records, path)
            self.assertTrue(os.path.exists(path))
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().strip().splitlines()
            self.assertEqual(len(lines), len(self.records) + 1)
            header = lines[0].split(",")
            for field in normalize.CANONICAL_FIELDS:
                self.assertIn(field, header, msg=field)

    def test_refuses_empty_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                normalize.write_records([], os.path.join(tmp, "out.csv"))

    def test_parquet_schema_types_are_stable(self):
        """Parsed types must be in the file, not just in the objects we passed:
        integers as BIGINT (not VARCHAR), measurements as DOUBLE. A regression
        here hands strings to the RL layer and nothing downstream complains."""
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "types.parquet")
            normalize.write_records(self.records, path)
            con = duckdb.connect()
            try:
                schema = {
                    row[0]: row[1]
                    for row in con.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{path.replace(chr(92), '/')}')"
                    ).fetchall()
                }
            finally:
                con.close()

            for field in normalize.NUMERIC_FIELDS:
                self.assertEqual(schema[field], "DOUBLE", msg=f"{field} -> {schema[field]}")
            for field in normalize.INT_FIELDS:
                self.assertEqual(schema[field], "BIGINT", msg=f"{field} -> {schema[field]}")
            for field in ("source_id", "cell_id", "step_type"):
                self.assertEqual(schema[field], "VARCHAR", msg=f"{field} -> {schema[field]}")

    def test_parquet_roundtrip_when_duckdb_present(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "canonical.parquet")
            normalize.write_records(self.records, path)
            self.assertTrue(os.path.getsize(path) > 0)

            import duckdb
            con = duckdb.connect()
            try:
                n, vmax, tmax = con.execute(
                    f"SELECT count(*), max(voltage_v), max(timestamp_s) "
                    f"FROM read_parquet('{path.replace(chr(92), '/')}')"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(n, len(self.records))
            self.assertAlmostEqual(vmax, 4.2, places=6)     # 4200 mV -> 4.2 V
            self.assertAlmostEqual(tmax, 300.0, places=6)   # 300000 ms -> 300 s

    def test_parquet_types_survive_an_all_null_numeric_column(self):
        """A column that is all NULL in the first batch (e.g. charge capacity on
        a discharge-only export) must still land as a float type, or appending
        the next batch silently truncates it."""
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb not installed")
        records = [
            {"cell_id": "a", "source_id": "x", "cycle_index": 1, "step_index": 1,
             "step_type": "discharge", "step_family": "discharge", "timestamp_s": 0.0,
             "voltage_v": 3.9, "current_a": 1.5, "temperature_c": 25.0,
             "charge_capacity_ah": None, "discharge_capacity_ah": 0.1},
            {"cell_id": "a", "source_id": "x", "cycle_index": 1, "step_index": 2,
             "step_type": "charge", "step_family": "charge", "timestamp_s": 60.0,
             "voltage_v": 4.1, "current_a": 1.5, "temperature_c": 25.1,
             "charge_capacity_ah": 0.123456, "discharge_capacity_ah": 0.2},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nulls.parquet")
            normalize.write_records(records, path)
            con = duckdb.connect()
            try:
                coltype = con.execute(
                    f"SELECT typeof(charge_capacity_ah) "
                    f"FROM read_parquet('{path.replace(chr(92), '/')}') LIMIT 1"
                ).fetchone()[0]
                value = con.execute(
                    f"SELECT max(charge_capacity_ah) "
                    f"FROM read_parquet('{path.replace(chr(92), '/')}')"
                ).fetchone()[0]
            finally:
                con.close()
            self.assertEqual(coltype, "DOUBLE", coltype)
            self.assertAlmostEqual(value, 0.123456, places=6)


# --------------------------------------------------------------------------- #
# Registry and licence gate
# --------------------------------------------------------------------------- #

class TestRegistry(unittest.TestCase):
    def test_all_ids_unique_and_resolvable(self):
        ids = [r.id for r in registry.RELEASES]
        self.assertEqual(len(ids), len(set(ids)))
        for rid in ids:
            self.assertEqual(registry.get(rid).id, rid)

    def test_unknown_id_lists_the_known_ones(self):
        with self.assertRaises(KeyError) as ctx:
            registry.get("nope")
        self.assertIn("nasa_pcoe_randomized", str(ctx.exception))

    def test_target_corpus_is_flagged_not_redistributable(self):
        corpus = registry.get("field_corpus_2026")
        self.assertEqual(corpus.kind, "field")
        self.assertFalse(corpus.redistributable)
        self.assertEqual(corpus.cells, 131096)

    def test_publish_gate(self):
        allowed, _ = fetch.can_publish_raw("nasa_pcoe_randomized")
        self.assertTrue(allowed)
        allowed, why = fetch.can_publish_raw("field_corpus_2026")
        self.assertFalse(allowed)
        self.assertIn("NOT redistributable", why)

    def test_filters(self):
        self.assertTrue(all(r.kind == "field" for r in registry.filter_releases(kind="field")))
        self.assertTrue(all(r.redistributable for r in registry.filter_releases(redistributable_only=True)))
        self.assertTrue(registry.filter_releases(tag="smoke-test"))

    def test_render_table_mentions_every_release(self):
        text = registry.render_table(registry.RELEASES)
        for rel in registry.RELEASES:
            self.assertIn(rel.id, text)


# --------------------------------------------------------------------------- #
# Resumable state
# --------------------------------------------------------------------------- #

class TestJobState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            st = state_mod.JobState(job="ingest")
            st.start("rel_a")
            st.finish("rel_a", rows_out=123, output="a.parquet")
            state_mod.save(st, path)

            reloaded = state_mod.load(path)
            self.assertEqual(reloaded.job, "ingest")
            self.assertEqual(reloaded.units["rel_a"].status, state_mod.DONE)
            self.assertEqual(reloaded.units["rel_a"].rows_out, 123)

    def test_missing_file_is_an_empty_state(self):
        st = state_mod.load(os.path.join(tempfile.gettempdir(), "definitely-not-here-12345.json"))
        self.assertEqual(st.units, {})
        self.assertFalse(st.is_complete())

    def test_pending_excludes_finished_units(self):
        st = state_mod.JobState(job="x")
        st.finish("a")
        st.skip("b")
        st.start("c")
        pending = st.pending(["a", "b", "c", "d"])
        self.assertEqual(pending, ["c", "d"])

    def test_failed_units_retry_until_the_cap(self):
        st = state_mod.JobState(job="x")
        st.meta["max_attempts"] = 2
        st.fail("a", "boom")
        self.assertIn("a", st.pending(["a"]))
        st.fail("a", "boom again")
        self.assertNotIn("a", st.pending(["a"]), "must stop retrying after the cap")

    def test_progress_and_completion(self):
        st = state_mod.JobState(job="x")
        st.finish("a")
        st.skip("b")
        st.start("c")
        self.assertFalse(st.is_complete())
        self.assertIn("2/3 complete", st.progress())
        st.finish("c")
        self.assertTrue(st.is_complete())

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            st = state_mod.JobState(job="x")
            for i in range(5):
                st.finish(f"unit{i}")
                state_mod.save(st, path)
            leftovers = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class TestCli(unittest.TestCase):
    def test_list(self):
        from battery_field_agent import cli
        self.assertEqual(cli.main(["list"]), 0)

    def test_inspect_returns_zero_on_a_resolvable_fixture(self):
        from battery_field_agent import cli
        rc = cli.main(["inspect", os.path.join(FIXTURES, "messy_mv_ma.csv")])
        self.assertEqual(rc, 0)

    def test_inspect_returns_two_when_required_columns_are_missing(self):
        from battery_field_agent import cli
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "partial.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Cycle,Temperature\n1,24\n")
            self.assertEqual(cli.main(["inspect", path]), 2)

    def test_normalize_writes_and_updates_state(self):
        from battery_field_agent import cli
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "canonical.csv")
            st = os.path.join(tmp, "state.json")
            rc = cli.main([
                "normalize", os.path.join(FIXTURES, "messy_mv_ma.csv"),
                "-o", out, "--source-id", "nasa_pcoe_randomized", "--cell-id", "B0005",
                "--state", st,
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))
            self.assertTrue(os.path.exists(st))
            self.assertEqual(cli.main(["status", "--state", st]), 0)

    def test_normalize_refuses_a_public_write_for_a_non_redistributable_source(self):
        from battery_field_agent import cli
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.main([
                "normalize", os.path.join(FIXTURES, "messy_mv_ma.csv"),
                "-o", os.path.join(tmp, "x.csv"),
                "--source-id", "field_corpus_2026", "--public",
            ])
            self.assertEqual(rc, 3)

    def test_bad_map_flag_is_rejected(self):
        from battery_field_agent import cli
        with self.assertRaises(SystemExit):
            cli.main(["inspect", os.path.join(FIXTURES, "messy_mv_ma.csv"), "--map", "bogus"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
