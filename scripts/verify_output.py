"""Independent verification of the canonical Parquet output.

Reads the artifact back with duckdb only -- no code from this package -- so it
checks the file rather than the pipeline's opinion of the file.
"""
import duckdb

PATH = "out/canonical.parquet"

con = duckdb.connect()
print("schema:")
for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{PATH}')").fetchall():
    print(f"   {name:<24} {dtype}")

print()
print("rows (canonical: positive current == discharge):")
print(con.execute(f"""
    SELECT cycle_index, step_index, step_type, step_family,
           timestamp_s, voltage_v, current_a, temperature_c, discharge_capacity_ah
    FROM read_parquet('{PATH}')
    ORDER BY timestamp_s
""").df().to_string(index=False))

print()
agg = con.execute(f"""
    SELECT count(*) AS n, count(DISTINCT cell_id) AS cells,
           round(min(voltage_v), 4) AS v_min, round(max(voltage_v), 4) AS v_max,
           round(max(timestamp_s), 1) AS t_max_s,
           round(sum(CASE WHEN step_family = 'discharge' THEN current_a ELSE 0 END), 3) AS disc_a
    FROM read_parquet('{PATH}')
""").fetchone()
print(f"aggregate: rows={agg[0]} cells={agg[1]} "
      f"v=[{agg[2]}, {agg[3]}] V  t_max={agg[4]} s  sum(discharge A)={agg[5]}")
con.close()
print()
print("checks:")
con = duckdb.connect()
n_null, = con.execute(
    f"SELECT count(*) FROM read_parquet('{PATH}') WHERE voltage_v IS NULL OR current_a IS NULL"
).fetchone()
neg_charge, = con.execute(
    f"SELECT count(*) FROM read_parquet('{PATH}') WHERE step_family='charge' AND current_a >= 0"
).fetchone()
pos_discharge, = con.execute(
    f"SELECT count(*) FROM read_parquet('{PATH}') WHERE step_family='discharge' AND current_a <= 0"
).fetchone()
con.close()
print(f"  null voltage/current rows        : {n_null}  (expect 0)")
print(f"  charge rows with current >= 0    : {neg_charge}  (expect 0)")
print(f"  discharge rows with current <= 0 : {pos_discharge}  (expect 0)")
print("RESULT:", "PASS" if (n_null, neg_charge, pos_discharge) == (0, 0, 0) else "FAIL")
