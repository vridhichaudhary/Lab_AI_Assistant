"""
lab_ingest.py
=============
Loads one or more PX/PTA lab-report .htm files into a SQLite table
(`lab_results`), so the chatbot can answer structured lab queries without
re-parsing HTML on every request.

Schema (table: lab_results)
----------------------------
    id          INTEGER PRIMARY KEY
    material    TEXT   -- e.g. "PNPX1 ( PX-1 )"
    sample      TEXT   -- e.g. "Benzene Product"
    date        TEXT   -- dd.mm.yyyy, as printed in the report
    shift       TEXT   -- 'M' | 'E' | 'N'
    parameter   TEXT   -- e.g. "BZ", "Unk", "C9A"
    unit        TEXT   -- e.g. "WT%", "PPM" (may be blank)
    value       TEXT   -- kept as text (values are already formatted strings
                           in the source report, e.g. "0.00", "100.00")
    source_file TEXT

This is intentionally a separate table/pipeline from the PDF-based
`documents`/ChromaDB pipeline in the main RAG project (see integration
notes at the bottom of this file and in ANTIGRAVITY_INTEGRATION.md) -
lab data is structured and needs exact lookups, not semantic search.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lab_parser import LabRecord, parse_lab_reports

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lab_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    material    TEXT NOT NULL,
    sample      TEXT NOT NULL,
    date        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    parameter   TEXT NOT NULL,
    unit        TEXT,
    value       TEXT NOT NULL,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_sample ON lab_results(sample);
CREATE INDEX IF NOT EXISTS idx_lab_date ON lab_results(date);
CREATE INDEX IF NOT EXISTS idx_lab_shift ON lab_results(shift);
"""


def init_lab_table(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def load_records_into_db(records: list[LabRecord], db_path: str | Path) -> int:
    """Insert parsed LabRecords into the lab_results table (long format:
    one row per parameter per sample/date/shift). Returns the number of
    parameter rows inserted.
    """
    init_lab_table(db_path)
    conn = sqlite3.connect(str(db_path))
    count = 0
    try:
        for r in records:
            for param, (unit, value) in r.values.items():
                conn.execute(
                    "INSERT INTO lab_results "
                    "(material, sample, date, shift, parameter, unit, value, source_file) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (r.material, r.sample, r.date, r.shift, param, unit, value, r.source_file),
                )
                count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def ingest_lab_reports(filepaths: list[str | Path], db_path: str | Path) -> dict:
    """End-to-end: parse HTML lab report(s) and load them into SQLite.

    Returns a small summary dict useful for a Streamlit status message.
    """
    records = parse_lab_reports(filepaths)
    inserted = load_records_into_db(records, db_path)
    return {
        "files": [str(f) for f in filepaths],
        "num_rows_parsed": len(records),
        "num_parameter_values_inserted": inserted,
        "unique_samples": len({r.sample for r in records}),
        "unique_materials": len({r.material for r in records}),
        "date_range": sorted({r.date for r in records}),
    }


def load_records_from_db(db_path: str | Path) -> list[LabRecord]:
    """Reconstruct LabRecord objects from the SQLite table (long format ->
    grouped back into one record per material/sample/date/shift), so
    lab_query.py can operate on them the same way whether they came
    straight from parsing or were reloaded from storage.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM lab_results ORDER BY material, sample, date, shift"
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[tuple, LabRecord] = {}
    for row in rows:
        key = (row["material"], row["sample"], row["date"], row["shift"], row["source_file"])
        if key not in grouped:
            grouped[key] = LabRecord(
                material=row["material"],
                sample=row["sample"],
                date=row["date"],
                shift=row["shift"],
                source_file=row["source_file"],
                values={},
            )
        grouped[key].values[row["parameter"]] = (row["unit"] or "", row["value"])

    return list(grouped.values())
