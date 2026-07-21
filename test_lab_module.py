"""
test_lab_module.py
===================
Unit tests for lab_parser.py and lab_query.py, run against the two real
sample report files. These pin down the exact behaviour verified during
development (including the user's own worked example) so future changes
can't silently regress it.

Run with: pytest -v test_lab_module.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from lab_parser import parse_lab_reports
from lab_query import query_records, parse_query, format_records_as_tables

SAMPLE_FILES = [
    "/mnt/user-data/uploads/PX_PTA_LAB_RESULTS_NIGHT_SHIFT_DATED_05_07_2026.htm",
    "/mnt/user-data/uploads/PX_PTA_LAB_RESULTS_NIGHT_SHIFT_DATED_07_07_2026.htm",
]


@pytest.fixture(scope="module")
def records():
    return parse_lab_reports(SAMPLE_FILES)


def test_parses_without_garbage_rows(records):
    """No sample name should ever be a bare number or punctuation - that
    would indicate a page-break/column-alignment parsing bug.
    """
    for r in records:
        assert any(c.isalpha() for c in r.sample), f"Suspicious sample name: {r.sample!r}"


def test_only_valid_shift_codes(records):
    for r in records:
        assert r.shift in {"M", "E", "N"}, f"Unexpected shift code: {r.shift!r}"


def test_no_boilerplate_leakage_into_values(records):
    banned = ("Report ID", "User:", "INDIAN OIL", "QC")
    for r in records:
        for _, (unit, value) in r.values.items():
            assert not any(b in value for b in banned)
            assert not any(b in unit for b in banned)


def test_worked_example_benzene_product(records):
    """The exact example from the project spec:
        'Benzene Product 7 July M' ->
            Unk   WT%  0.00
            C9A   WT%  0.00
            C10A  WT%  0.00
            BZ    WT%  100.00
    """
    results, parsed, warnings = query_records("Benzene Product 7 July M", records)
    assert warnings == []
    assert len(results) == 1
    r = results[0]
    assert r.sample == "Benzene Product"
    assert r.date == "07.07.2026"
    assert r.shift == "M"
    assert r.values["Unk"] == ("WT%", "0.00")
    assert r.values["C9A"] == ("WT%", "0.00")
    assert r.values["C10A"] == ("WT%", "0.00")
    assert r.values["BZ"] == ("WT%", "100.00")


def test_missing_shift_returns_all_shifts_for_that_day(records):
    results, parsed, _ = query_records("Benzene Product 7 July", records)
    assert parsed.shift is None
    # Every returned row must be on the requested date, any shift.
    assert all(r.date == "07.07.2026" for r in results)


def test_explicit_shift_filters_correctly(records):
    results, parsed, warnings = query_records("Reformate 7 July E", records)
    if results:
        assert all(r.shift == "E" for r in results)
    # If no E-shift Reformate row exists that day, a clear warning must fire.
    if not results:
        assert warnings


def test_unknown_sample_gives_clear_warning(records):
    results, parsed, warnings = query_records("Totally Fake Sample 7 July", records)
    assert results == []
    assert any("No sample name matching" in w for w in warnings)


def test_date_parsing_variants():
    for q in ["7 July 2026", "July 7 2026", "07.07.2026", "07-07-2026", "7th July"]:
        p = parse_query(q)
        assert p.day == 7 and p.month == 7, f"Failed on: {q}"


def test_shift_word_not_confused_with_sample_name():
    # "Reformate" contains the letter sequence... ensure shift extraction
    # only matches whole words, not substrings inside sample names.
    p = parse_query("Reformate 7 July")
    assert p.shift is None
    assert "Reformate" in (p.sample_hint or "")


def test_format_output_matches_pipe_table_style(records):
    results, _, _ = query_records("Benzene Product 7 July M", records)
    out = format_records_as_tables(results)
    assert "| Parameter | Unit | Value |" in out
    assert "| BZ | WT% | 100.00 |" in out
