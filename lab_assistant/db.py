"""
db.py — SQLite database schema, CRUD helpers, and 7-day auto-cleanup.
"""
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lab_results.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reports (
            report_id        TEXT PRIMARY KEY,
            upload_date      TEXT NOT NULL,
            report_date      TEXT NOT NULL,
            shift            TEXT NOT NULL,
            uploaded_by      TEXT,
            original_file_name TEXT,
            file_path        TEXT,
            expires_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lab_results (
            result_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id        TEXT    NOT NULL,
            report_date      TEXT    NOT NULL,
            shift            TEXT    NOT NULL,
            sample_name      TEXT,
            parameter_name   TEXT    NOT NULL,
            parameter_value  TEXT    NOT NULL,
            FOREIGN KEY (report_id) REFERENCES reports(report_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_lr_report    ON lab_results(report_id);
        CREATE INDEX IF NOT EXISTS idx_lr_sample    ON lab_results(sample_name);
        CREATE INDEX IF NOT EXISTS idx_lr_param     ON lab_results(parameter_name);
        CREATE INDEX IF NOT EXISTS idx_r_date_shift ON lab_results(report_date, shift);
    """)
    conn.commit()
    conn.close()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def insert_report(report_date: str, shift: str, uploaded_by: str,
                  original_file_name: str, file_path: str) -> str:
    """Insert a report row. Returns the new report_id."""
    now = datetime.now()
    report_id = str(uuid.uuid4())
    expires_at = (now + timedelta(days=7)).isoformat()
    conn = get_conn()
    conn.execute(
        """INSERT INTO reports
           (report_id, upload_date, report_date, shift, uploaded_by,
            original_file_name, file_path, expires_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (report_id, now.isoformat(), report_date, shift, uploaded_by,
         original_file_name, str(file_path), expires_at)
    )
    conn.commit()
    conn.close()
    return report_id


def insert_lab_results(report_id: str, rows: list[dict]):
    """
    Bulk-insert parsed lab result rows.
    Each row: {report_date, shift, sample_name, parameter_name, parameter_value}
    Blank values must be filtered BEFORE calling this.
    """
    if not rows:
        return
    conn = get_conn()
    conn.executemany(
        """INSERT INTO lab_results 
           (report_id, report_date, shift, sample_name, parameter_name, parameter_value)
           VALUES (:report_id, :report_date, :shift, :sample_name, :parameter_name, :parameter_value)""",
        [{"report_id": report_id, **r} for r in rows]
    )
    conn.commit()
    conn.close()


def get_all_reports() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT r.*,
               COUNT(lr.result_id) AS result_count
        FROM   reports r
        LEFT JOIN lab_results lr ON r.report_id = lr.report_id
        GROUP  BY r.report_id
        ORDER  BY r.upload_date DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_report(report_id: str):
    """Delete a report and its lab data + the physical file."""
    conn = get_conn()
    row = conn.execute(
        "SELECT file_path FROM reports WHERE report_id=?", (report_id,)
    ).fetchone()
    if row and row["file_path"]:
        fp = Path(row["file_path"])
        if fp.exists():
            fp.unlink()
    conn.execute("DELETE FROM reports WHERE report_id=?", (report_id,))
    conn.commit()
    conn.close()


def query_results(report_date: str | None = None,
                  shift: str | None = None,
                  sample_filter: str | None = None,
                  parameter_filter: str | None = None) -> list[dict]:
    """
    Flexible query function used by the chatbot.
    Returns non-empty parameter records only (they are always non-empty by design).
    """
    conn = get_conn()
    sql = """
        SELECT lr.report_date, lr.shift, lr.sample_name,
               lr.parameter_name, lr.parameter_value
        FROM   lab_results lr
        WHERE  1=1
    """
    params: list = []
    if report_date:
        sql += " AND lr.report_date = ?"
        params.append(report_date)
    if shift:
        sql += " AND UPPER(lr.shift) = ?"
        params.append(shift.upper())
    if sample_filter:
        sql += " AND LOWER(lr.sample_name) LIKE ?"
        params.append(f"%{sample_filter.lower()}%")
    if parameter_filter:
        sql += " AND LOWER(lr.parameter_name) LIKE ?"
        params.append(f"%{parameter_filter.lower()}%")
    sql += " ORDER BY lr.report_date DESC, lr.shift, lr.sample_name, lr.parameter_name"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 7-day automatic cleanup ───────────────────────────────────────────────────

def run_cleanup() -> int:
    """Delete all reports older than 7 days. Returns count deleted."""
    now = datetime.now().isoformat()
    conn = get_conn()
    expired = conn.execute(
        "SELECT report_id, file_path FROM reports WHERE expires_at < ?", (now,)
    ).fetchall()

    for row in expired:
        fp = Path(row["file_path"]) if row["file_path"] else None
        if fp and fp.exists():
            fp.unlink()
        conn.execute("DELETE FROM reports WHERE report_id=?", (row["report_id"],))

    conn.commit()
    conn.close()
    return len(expired)
