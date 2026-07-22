"""
lab_ingest.py
=============
Loads PX/PTA lab-report .htm files into PostgreSQL (Supabase) and uploads
the physical file to Supabase Storage for 7-day persistent retention.

This replaces the previous SQLite + local filesystem implementation.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from supabase import create_client, Client

from lab_parser import LabRecord, parse_lab_reports

load_dotenv()

DATABASE_URL   = os.getenv("DATABASE_URL", "")
SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY", "")
STORAGE_BUCKET = "lab-reports"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS structured_reports (
    source_file  TEXT PRIMARY KEY,
    upload_date  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    storage_path TEXT
);

CREATE TABLE IF NOT EXISTS structured_lab_results (
    id          SERIAL PRIMARY KEY,
    material    TEXT NOT NULL,
    sample      TEXT NOT NULL,
    date        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    parameter   TEXT NOT NULL,
    unit        TEXT,
    value       TEXT NOT NULL,
    source_file TEXT NOT NULL REFERENCES structured_reports(source_file) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_slab_sample ON structured_lab_results(sample);
CREATE INDEX IF NOT EXISTS idx_slab_date   ON structured_lab_results(date);
CREATE INDEX IF NOT EXISTS idx_slab_shift  ON structured_lab_results(shift);
"""


def _get_conn() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def _get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def init_lab_table(db_path=None) -> None:
    """db_path kept for backward-compat signature; ignored (uses DATABASE_URL)."""
    conn = _get_conn()
    cur  = conn.cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()
    cur.close()
    conn.close()


def cleanup_structured_reports(db_path=None, uploads_dir=None) -> int:
    """Delete structured reports older than 7 days from DB and Supabase Storage."""
    now  = datetime.now().isoformat()
    conn = _get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT source_file, storage_path FROM structured_reports WHERE expires_at < %s",
        (now,)
    )
    expired = cur.fetchall()

    sb = _get_supabase() if expired else None
    for row in expired:
        sp = row.get("storage_path")
        if sp:
            try:
                sb.storage.from_(STORAGE_BUCKET).remove([sp])
            except Exception:
                pass
        cur.execute(
            "DELETE FROM structured_reports WHERE source_file=%s",
            (row["source_file"],)
        )

    conn.commit()
    cur.close()
    conn.close()
    return len(expired)


def delete_structured_report(source_file: str):
    """Manually delete a specific structured report from DB and Supabase Storage."""
    conn = _get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT storage_path FROM structured_reports WHERE source_file = %s",
        (source_file,)
    )
    row = cur.fetchone()
    
    if row:
        sp = row.get("storage_path")
        if sp:
            try:
                sb = _get_supabase()
                sb.storage.from_(STORAGE_BUCKET).remove([sp])
            except Exception as e:
                print(f"[warn] Failed to delete from Supabase: {e}")
                
        cur.execute(
            "DELETE FROM structured_reports WHERE source_file=%s",
            (source_file,)
        )
        conn.commit()
        
    cur.close()
    conn.close()


def load_records_into_db(records: list[LabRecord], db_path=None,
                         file_bytes: bytes | None = None,
                         source_filename: str = "") -> int:
    """Insert parsed LabRecords into PostgreSQL. Optionally upload raw bytes to Supabase Storage."""
    conn = _get_conn()
    cur  = conn.cursor()
    now        = datetime.now()
    expires_at = (now + timedelta(days=7)).isoformat()

    unique_files = {r.source_file for r in records}
    sb = _get_supabase()

    for sf in unique_files:
        storage_path = ""
        # Upload the physical .htm file to Supabase Storage
        if file_bytes:
            storage_path = f"{sf}"
            try:
                sb.storage.from_(STORAGE_BUCKET).upload(
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": "text/html", "upsert": "true"},
                )
            except Exception as e:
                print(f"[warn] Supabase Storage upload failed: {e}")

        cur.execute(
            """INSERT INTO structured_reports (source_file, upload_date, expires_at, storage_path)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (source_file) DO UPDATE
               SET upload_date=%s, expires_at=%s, storage_path=%s""",
            (sf, now.isoformat(), expires_at, storage_path,
             now.isoformat(), expires_at, storage_path)
        )

    count = 0
    for r in records:
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO structured_lab_results
               (material, sample, date, shift, parameter, unit, value, source_file)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(r.material, r.sample, r.date, r.shift, param, unit, value, r.source_file)
             for param, (unit, value) in r.values.items()]
        )
        count += len(r.values)

    conn.commit()
    cur.close()
    conn.close()
    return count


def ingest_lab_reports(filepaths: list, db_path=None,
                       file_bytes: bytes | None = None) -> dict:
    """End-to-end: parse HTML lab report(s) and load into PostgreSQL + Supabase Storage."""
    records  = parse_lab_reports(filepaths)
    source_filename = str(Path(filepaths[0]).name) if filepaths else ""
    inserted = load_records_into_db(records, file_bytes=file_bytes,
                                    source_filename=source_filename)
    return {
        "files": [str(f) for f in filepaths],
        "num_rows_parsed": len(records),
        "num_parameter_values_inserted": inserted,
        "unique_samples": len({r.sample for r in records}),
        "unique_materials": len({r.material for r in records}),
        "date_range": sorted({r.date for r in records}),
    }


def load_records_from_db(db_path=None) -> list[LabRecord]:
    """Load all LabRecords from PostgreSQL, reconstructed from long-format rows."""
    conn = _get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM structured_lab_results ORDER BY material, sample, date, shift"
    )
    rows = cur.fetchall()
    cur.close()
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


def get_all_structured_reports() -> list[dict]:
    """Return all stored structured reports with expiry info for the UI."""
    conn = _get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT sr.source_file, sr.upload_date, sr.expires_at,
               COUNT(slr.id) AS record_count,
               MIN(slr.date) AS earliest_date,
               MAX(slr.date) AS latest_date,
               STRING_AGG(DISTINCT slr.shift, '/') AS shifts
        FROM   structured_reports sr
        LEFT JOIN structured_lab_results slr ON sr.source_file = slr.source_file
        GROUP  BY sr.source_file, sr.upload_date, sr.expires_at
        ORDER  BY sr.upload_date DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]
