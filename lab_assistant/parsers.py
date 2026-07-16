"""
parsers.py — File parsers for HTML, Excel/CSV, and PDF lab report files.

Rules:
- Dynamic column detection (no hardcoded parameter names).
- Blank/null values are NEVER stored.
- Each non-empty cell → one {report_date, shift, sample_name, parameter_name, parameter_value} record.
"""
import re
import io
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup


# ── Utility ───────────────────────────────────────────────────────────────────

_BLANK = {"", "nan", "none", "-", "--", "n/a", "na", "#n/a", "nil", "unnamed"}

def _clean(v) -> str | None:
    """Return None if blank, else return string value."""
    if pd.isna(v) or v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in _BLANK or s.lower().startswith("unnamed:"):
        return None
    return s


def _format_date(raw_date: str, fallback: str | None) -> str | None:
    if not raw_date:
        return fallback
    # dd.mm.yyyy or dd/mm/yyyy
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", raw_date)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
    # yyyy-mm-dd
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
    if m:
        return m.group(0)
    return fallback


def _format_shift(raw_shift: str, fallback: str | None) -> str | None:
    if not raw_shift:
        return fallback
    s = raw_shift.strip().upper()
    if s in ("M", "MORNING"):
        return "M"
    if s in ("E", "EVENING"):
        return "E"
    return fallback


def _df_to_rows(df: pd.DataFrame, default_date: str | None, default_shift: str | None) -> list[dict]:
    """
    Convert a DataFrame to a list of lab result rows.
    Dynamically finds the header row containing "SAMPLE" and extracts
    row-level Date and Shift if present.
    """
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty or df.shape[1] < 2:
        return []

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(c) for c in col if str(c) != "nan").strip() for col in df.columns]
    
    # 1. Find the actual header row
    header_idx = -1
    for i, row in df.iterrows():
        # Check if this row contains 'SAMPLE' in any cell
        for cell in row:
            if isinstance(cell, str) and "SAMPLE" in cell.upper():
                header_idx = i
                break
        if header_idx != -1:
            break

    if header_idx != -1:
        # We found a header row inside the data!
        new_header = df.iloc[header_idx]
        df = df.iloc[header_idx + 1:]
        df.columns = new_header
        
    df.columns = [str(c).strip() for c in df.columns]

    # 2. Identify core columns
    cols_upper = [c.upper() for c in df.columns]
    
    sample_col_idx = -1
    date_col_idx = -1
    shift_col_idx = -1
    
    for i, c in enumerate(cols_upper):
        if "SAMPLE" in c and sample_col_idx == -1:
            sample_col_idx = i
        elif "DATE" in c and date_col_idx == -1:
            date_col_idx = i
        elif "SHIFT" in c and shift_col_idx == -1:
            shift_col_idx = i
            
    if sample_col_idx == -1:
        # Fallback: assume first column is sample
        sample_col_idx = 0

    sample_col_name = df.columns[sample_col_idx]
    
    rows = []
    for _, row in df.iterrows():
        sample = _clean(row[sample_col_name])
        if not sample:
            continue

        # Extract row-level Date and Shift if columns exist
        r_date = default_date
        r_shift = default_shift
        
        if date_col_idx != -1:
            r_date = _format_date(_clean(row[df.columns[date_col_idx]]) or "", default_date)
        if shift_col_idx != -1:
            r_shift = _format_shift(_clean(row[df.columns[shift_col_idx]]) or "", default_shift)

        # Iterate over all other columns (parameters)
        for i, col_name in enumerate(df.columns):
            if i in (sample_col_idx, date_col_idx, shift_col_idx):
                continue
                
            val = _clean(row[col_name])
            if val is None:
                continue
                
            param_name = str(col_name).strip()
            if not param_name or param_name.lower() in _BLANK or param_name.isnumeric():
                continue
                
            rows.append({
                "report_date":     r_date or "Unknown",
                "shift":           r_shift or "Unknown",
                "sample_name":     sample,
                "parameter_name":  param_name,
                "parameter_value": val,
            })
    return rows


# ── Auto-detect shift & date from filename / content ─────────────────────────

_SHIFT_RE = re.compile(r"\b(morning|evening|m\b|e\b)\b", re.I)
_DATE_RE  = re.compile(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})")   # dd.mm.yyyy

def detect_metadata(file_name: str, content_text: str = "") -> dict:
    """
    Try to extract shift and report_date from the filename and/or file content.
    Returns dict with keys 'shift' (None/'M'/'E') and 'report_date' (None/ISO str).
    """
    text = file_name + " " + content_text

    # Shift
    shift = None
    m = _SHIFT_RE.search(text)
    if m:
        w = m.group(1).lower()
        shift = "E" if w in ("evening", "e") else "M"

    # Date (dd.mm.yyyy)
    report_date = None
    dm = _DATE_RE.search(text)
    if dm:
        dd, mm, yyyy = dm.groups()
        report_date = f"{yyyy}-{mm}-{dd}"   # ISO

    return {"shift": shift, "report_date": report_date}


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_html(file_bytes: bytes, file_name: str) -> tuple[list[dict], dict]:
    """Parse .html / .htm lab report. Returns (rows, detected_metadata)."""
    text = file_bytes.decode("utf-8", errors="ignore")
    meta = detect_metadata(file_name, text)

    rows = []
    try:
        tables = pd.read_html(io.StringIO(text), flavor="lxml")
    except Exception:
        tables = []

    for df in tables:
        rows.extend(_df_to_rows(df, meta.get("report_date"), meta.get("shift")))

    return rows, meta


def parse_excel(file_bytes: bytes, file_name: str) -> tuple[list[dict], dict]:
    """Parse .xlsx / .xls lab report. Returns (rows, detected_metadata)."""
    meta = detect_metadata(file_name)

    rows = []
    try:
        xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
        for _sheet, df in xls.items():
            rows.extend(_df_to_rows(df, meta.get("report_date"), meta.get("shift")))
    except Exception:
        pass

    return rows, meta


def parse_csv(file_bytes: bytes, file_name: str) -> tuple[list[dict], dict]:
    """Parse .csv lab report."""
    meta = detect_metadata(file_name)
    rows = []
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), header=None)
        rows.extend(_df_to_rows(df, meta.get("report_date"), meta.get("shift")))
    except Exception:
        pass
    return rows, meta


def parse_pdf(file_bytes: bytes, file_name: str) -> tuple[list[dict], dict]:
    """Parse PDF lab report using pdfplumber."""
    import pdfplumber

    meta = detect_metadata(file_name)
    content_text = ""
    rows = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            content_text += page.extract_text() or ""
            for table in (page.extract_tables() or []):
                if not table or len(table) < 2:
                    continue
                df = pd.DataFrame(table)
                rows.extend(_df_to_rows(df, meta.get("report_date"), meta.get("shift")))

    if not meta["shift"] or not meta["report_date"]:
        extra = detect_metadata(file_name, content_text)
        meta["shift"] = meta["shift"] or extra["shift"]
        meta["report_date"] = meta["report_date"] or extra["report_date"]

    return rows, meta


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_file(file_bytes: bytes, file_name: str) -> tuple[list[dict], dict]:
    """Route file to correct parser. Returns (rows, detected_metadata)."""
    ext = Path(file_name).suffix.lower().lstrip(".")
    if ext in ("html", "htm"):
        return parse_html(file_bytes, file_name)
    elif ext in ("xlsx", "xls"):
        return parse_excel(file_bytes, file_name)
    elif ext == "csv":
        return parse_csv(file_bytes, file_name)
    elif ext == "pdf":
        return parse_pdf(file_bytes, file_name)
    else:
        raise ValueError(f"Unsupported file type: .{ext}")
