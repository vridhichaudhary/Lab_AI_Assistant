"""
lab_parser.py
=============
Parses IOCL "Stream Analysis" lab-result HTML reports (PX/PTA plant QC
reports) into a flat, queryable long-format table.

Why a dedicated parser instead of generic PDF/RAG chunking?
-------------------------------------------------------------
These .htm files are NOT prose documents - they are wide, multi-material
lab data dumps rendered as monospaced text (one HTML <nobr> tag per
"cell"). Semantic/embedding-based RAG retrieval is the wrong tool for this:
a question like "Benzene Product 7 July M" needs an exact structured
lookup (sample + date + shift -> every reported parameter), not a fuzzy
nearest-neighbour text match. This module extracts the report into a
clean, fully structured table so that kind of question can be answered
with 100% precision instead of probabilistically.

File structure (discovered by inspecting real report HTML):
- The whole report is a sequence of <nobr> tags, each one holding exactly
  one "cell" of monospaced text (a header label, a unit, or a value).
- The report is divided into MATERIAL sections (e.g. "PNPX1 ( PX-1 )",
  "PNPX2 ( PX-2 )", "PNOX1 ( OXIDATION )", "PNPUR1 ( PURIFICATION )").
- Each section has: a header row (SAMPLE, DATE, SHIFT, <parameter names>),
  then a units row of the SAME length (blank for SAMPLE/DATE/SHIFT), then
  N data rows, each also of that same length.
- Column count (L) differs between sections (different materials measure
  different parameters), so L is computed per-section rather than assumed.

Row-length detection trick
---------------------------
Within a section, header/units/every data row all have identical length L.
The header row always starts "SAMPLE", "DATE", "SHIFT", <params...>. The
first data row's DATE cell (a dd.mm.yyyy string) sits at position
H + 2*L + 1, where H is the index of the header's "SAMPLE" cell. Solving
for L given the index D of the first date-like cell after H:

    L = (D - 1 - H) // 2

This lets the parser recover the column count for each section without
any hard-coded schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
NBSP = "\xa0"


@dataclass
class LabRecord:
    """One (material, sample, date, shift) row with all reported parameters."""

    material: str
    sample: str
    date: str  # dd.mm.yyyy as printed in the report
    shift: str  # 'M' | 'E' | 'N' (or whatever the report contains)
    source_file: str
    values: dict[str, tuple[str, str]] = field(default_factory=dict)
    # values: { parameter_name: (unit, value_str) }, present entries only


def _strip_page_break_boilerplate(cells: list[str]) -> list[str]:
    """Remove mid-table page-break boilerplate.

    These reports are paginated for printing, and each page break injects
    a footer/header block directly into the cell stream:
        "User: <printed-by> ... Date Time: ..."
        ""
        ""
        "Report ID: ... INDIAN OIL CORPORATION LIMITED ... Page: N"
        "QC"
        ""   (one or more trailing blank spacer cells)
    If left in place, this silently shifts every subsequent "cell index"
    within the section, corrupting the fixed row-length alignment used to
    recover section columns. The very first "Report ID:" cell (index 0,
    the report's own opening header) is legitimate and left untouched -
    only occurrences that follow a "User:" line (i.e. true page breaks)
    are stripped.
    """
    report_id_idxs = [i for i, c in enumerate(cells) if c.startswith("Report ID:")]
    user_idxs = [i for i, c in enumerate(cells) if c.startswith("User:")]

    remove_ranges: list[tuple[int, int]] = []
    for ridx in report_id_idxs:
        if ridx == 0:
            continue  # legitimate report-opening header, not a page break
        # Find the nearest preceding "User:" line (should be a few cells back).
        preceding_user = max((u for u in user_idxs if u < ridx), default=None)
        if preceding_user is None or ridx - preceding_user > 10:
            continue  # not the expected pattern; leave it alone defensively
        start = preceding_user
        end = ridx + 1  # the 'QC' line that follows Report ID
        # Also swallow any blank spacer cells immediately after 'QC'.
        while end + 1 < len(cells) and cells[end + 1] == "":
            end += 1
        remove_ranges.append((start, end))

    if not remove_ranges:
        return cells

    remove_idxs: set[int] = set()
    for start, end in remove_ranges:
        remove_idxs.update(range(start, end + 1))

    return [c for i, c in enumerate(cells) if i not in remove_idxs]


def _extract_cells(filepath: str | Path) -> list[str]:
    """Read every <nobr> cell from the report, in document order, with
    non-breaking spaces normalized to regular spaces and whitespace trimmed.
    Mid-table page-break boilerplate is stripped so column alignment stays
    correct across page boundaries.
    """
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    nobrs = soup.find_all("nobr")
    cells = [n.get_text().replace(NBSP, " ").strip() for n in nobrs]
    return _strip_page_break_boilerplate(cells)


def parse_lab_report(filepath: str | Path) -> list[LabRecord]:
    """Parse one lab-report .htm file into a list of LabRecord rows."""
    filepath = Path(filepath)
    cells = _extract_cells(filepath)

    material_idxs = [i for i, c in enumerate(cells) if c == "MATERIAL :"]
    section_bounds = material_idxs + [len(cells)]

    records: list[LabRecord] = []

    for k, m in enumerate(material_idxs):
        material_name = cells[m + 1]
        section_end = section_bounds[k + 1]

        # Locate header start H: first literal 'SAMPLE' cell after the
        # material name.
        header_start = None
        for i in range(m + 2, section_end):
            if cells[i] == "SAMPLE":
                header_start = i
                break
        if header_start is None:
            continue  # malformed/empty section, skip defensively

        # Locate the first date-like cell after the header to solve for L.
        first_date_idx = None
        for i in range(header_start + 1, section_end):
            if DATE_RE.match(cells[i]):
                first_date_idx = i
                break
        if first_date_idx is None:
            continue  # no data rows in this section

        row_len = (first_date_idx - 1 - header_start) // 2
        if row_len <= 3:
            continue  # sanity guard against a parsing anomaly

        header = cells[header_start : header_start + row_len]
        units = cells[header_start + row_len : header_start + 2 * row_len]
        data_start = header_start + 2 * row_len

        # Data rows are NOT always exactly row_len cells long: some rows
        # omit trailing blank cells (columns with nothing reported at the
        # very end of the row simply have no <nobr> tag at all in the
        # source HTML). Advancing by a fixed row_len would silently drift
        # out of alignment the first time this happens. Instead, find each
        # row's actual boundary by locating the next row's DATE cell
        # (always immediately after the next SAMPLE cell) and using that
        # to determine where the current row ends.
        row_starts = [data_start]
        search_from = data_start + 2  # skip this row's own SAMPLE/DATE
        while True:
            next_date_idx = None
            for i in range(search_from, section_end):
                if DATE_RE.match(cells[i]):
                    next_date_idx = i
                    break
            if next_date_idx is None:
                break
            next_row_start = next_date_idx - 1
            row_starts.append(next_row_start)
            search_from = next_row_start + 2

        row_bounds = list(zip(row_starts, row_starts[1:] + [section_end]))

        for row_start, row_end in row_bounds:
            row = cells[row_start:row_end]
            if len(row) < 3:
                continue
            sample, date, shift = row[0], row[1], row[2]
            if not sample or not re.search(r"[A-Za-z0-9]", sample):
                continue  # skip stray blank/decoration rows (e.g. '|' borders)

            values: dict[str, tuple[str, str]] = {}
            for h, u, v in zip(header[3:], units[3:], row[3:]):
                if h and v:  # only keep parameters that were actually reported
                    values[h] = (u, v)

            records.append(
                LabRecord(
                    material=material_name,
                    sample=sample,
                    date=date,
                    shift=shift,
                    source_file=filepath.name,
                    values=values,
                )
            )

    return records


def parse_lab_reports(filepaths: list[str | Path]) -> list[LabRecord]:
    """Parse multiple report files and return one combined list of records."""
    all_records: list[LabRecord] = []
    for fp in filepaths:
        all_records.extend(parse_lab_report(fp))
    return all_records
