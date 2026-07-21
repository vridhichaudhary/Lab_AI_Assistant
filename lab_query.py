"""
lab_query.py
============
Natural-language query engine over parsed LabRecord data (see
lab_parser.py). Answers questions like:

    "Benzene Product 7 July M"
    "DSN 5 July"                (no shift -> all shifts returned)
    "PX-1 Reformate night"

by resolving the sample name, date, and (optional) shift from the query
text, then returning every reported parameter for the matching row(s) as a
clean table: parameter | unit | value.

Shift codes: M = Morning, E = Evening, N = Night.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

from lab_parser import LabRecord

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_NAMES.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

SHIFT_WORDS = {
    "m": "M", "morning": "M",
    "e": "E", "evening": "E",
    "n": "N", "night": "N",
}

# Longest-match-first date patterns.
_DATE_PATTERNS = [
    # "7 July 2026", "07 July", "7th July"
    re.compile(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>[A-Za-z]+)\.?\s*(?P<year>\d{4})?\b"
    ),
    # "July 7", "July 7th"
    re.compile(
        r"\b(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*(?P<year>\d{4})?\b"
    ),
    # "07.07.2026", "07-07-2026", "07/07/2026"
    re.compile(r"\b(?P<day>\d{1,2})[./-](?P<month_num>\d{1,2})[./-](?P<year>\d{4})\b"),
]


@dataclass
class ParsedQuery:
    sample_hint: str | None
    day: int | None
    month: int | None
    year: int | None
    shift: str | None
    raw_query: str


def _extract_date(query: str) -> tuple[int | None, int | None, int | None, str]:
    """Return (day, month, year, remaining_query_with_date_removed)."""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(query)
        if not match:
            continue
        groups = match.groupdict()
        day = int(groups["day"]) if groups.get("day") else None
        year = int(groups["year"]) if groups.get("year") else None
        if "month_num" in groups and groups["month_num"]:
            month = int(groups["month_num"])
        else:
            month_str = (groups.get("month") or "").lower()
            month = MONTH_NAMES.get(month_str)
        if day and month:
            remaining = query[: match.start()] + " " + query[match.end() :]
            return day, month, year, remaining
    return None, None, None, query


def _extract_shift(query: str) -> tuple[str | None, str]:
    """Return (shift_code_or_None, remaining_query_with_shift_removed).

    Only matches shift words as whole tokens, so a sample name like
    "Reformate" is never mistaken for containing an 'e' shift token, etc.
    """
    tokens = re.findall(r"[A-Za-z]+", query)
    for tok in tokens:
        low = tok.lower()
        if low in SHIFT_WORDS and len(tok) <= len("morning"):
            # Require it to be an isolated word (not part of a longer word)
            # via word-boundary regex removal.
            pattern = re.compile(rf"\b{re.escape(tok)}\b")
            if pattern.search(query):
                remaining = pattern.sub(" ", query, count=1)
                return SHIFT_WORDS[low], remaining
    return None, query


def parse_query(query: str) -> ParsedQuery:
    day, month, year, remaining = _extract_date(query)
    shift, remaining = _extract_shift(remaining)
    sample_hint = re.sub(r"\s+", " ", remaining).strip(" ,.-") or None
    return ParsedQuery(sample_hint, day, month, year, shift, query)


def _best_sample_match(sample_hint: str | None, known_samples: list[str]) -> list[str]:
    """Return the known sample name(s) that best match the free-text hint.

    Strategy: exact case-insensitive match first; then substring containment
    in either direction; then a light token-overlap fallback. Returns a list
    because a hint could legitimately match more than one sample name (rare,
    but handled rather than silently dropped).
    """
    if not sample_hint:
        return []
    hint_low = sample_hint.lower().strip()
    if not hint_low:
        return []

    # 1) Exact match
    exact = [s for s in known_samples if s.lower() == hint_low]
    if exact:
        return exact

    # 2) Substring containment (hint inside sample name, or vice versa)
    contains = [
        s for s in known_samples
        if hint_low in s.lower() or s.lower() in hint_low
    ]
    if contains:
        # Prefer the longest / closest-length match
        contains.sort(key=lambda s: abs(len(s) - len(hint_low)))
        best_len = len(contains[0])
        return [s for s in contains if len(s) == best_len] or contains[:1]

    # 3) Token overlap fallback (e.g. "benzene" alone -> "Benzene Product")
    hint_tokens = set(hint_low.split())
    scored = []
    for s in known_samples:
        s_tokens = set(s.lower().split())
        overlap = len(hint_tokens & s_tokens)
        if overlap:
            scored.append((overlap, s))
    if scored:
        scored.sort(reverse=True)
        top_score = scored[0][0]
        return [s for score, s in scored if score == top_score]

    return []


def _date_matches(record_date: str, day: int | None, month: int | None, year: int | None) -> bool:
    if day is None or month is None:
        return True  # no date filter requested -> match all dates
    try:
        d_part, m_part, y_part = record_date.split(".")
        rd, rm, ry = int(d_part), int(m_part), int(y_part)
    except (ValueError, AttributeError):
        return False
    if rd != day or rm != month:
        return False
    if year is not None and ry != year:
        return False
    return True


def query_records(
    query: str, records: list[LabRecord]
) -> tuple[list[LabRecord], ParsedQuery, list[str]]:
    """Resolve a natural-language query against parsed lab records.

    Returns (matching_records, parsed_query, warnings). `warnings` lists
    any ambiguity or non-matches worth surfacing to the user (e.g. sample
    name not found, multiple candidate samples).
    """
    parsed = parse_query(query)
    warnings: list[str] = []

    known_samples = sorted({r.sample for r in records})
    matched_samples = _best_sample_match(parsed.sample_hint, known_samples)

    if parsed.sample_hint and not matched_samples:
        warnings.append(
            f"No sample name matching '{parsed.sample_hint}' was found in the "
            f"loaded lab reports."
        )
        return [], parsed, warnings

    if len(matched_samples) > 1:
        warnings.append(
            f"'{parsed.sample_hint}' matched multiple samples: "
            f"{', '.join(matched_samples)}. Showing results for all of them."
        )

    results = [
        r
        for r in records
        if (not matched_samples or r.sample in matched_samples)
        and _date_matches(r.date, parsed.day, parsed.month, parsed.year)
        and (parsed.shift is None or r.shift.upper() == parsed.shift)
    ]

    if not results:
        warnings.append(
            "No matching lab records were found for the given sample/date/shift "
            "combination. Please check the sample name, date, or shift and try again."
        )

    return results, parsed, warnings


SHIFT_FULL_NAME = {"M": "Morning", "E": "Evening", "N": "Night"}


def format_records_as_tables(results: list[LabRecord]) -> str:
    """Render matching LabRecords as markdown tables, one per
    (material, sample, date, shift) row - exactly the format the user
    demonstrated: parameter | unit | value.
    """
    if not results:
        return "No data found."

    blocks = []
    for r in results:
        shift_name = SHIFT_FULL_NAME.get(r.shift.upper(), r.shift)
        header = (
            f"**{r.sample}** — {r.date} — {shift_name} shift "
            f"({r.material}, source: {r.source_file})"
        )
        if not r.values:
            blocks.append(f"{header}\n\n_No parameters were reported for this row._")
            continue
        lines = [header, "", "| Parameter | Unit | Value |", "|---|---|---|"]
        for param, (unit, value) in r.values.items():
            lines.append(f"| {param} | {unit} | {value} |")
        blocks.append("\n".join(lines))

    return "\n\n---\n\n".join(blocks)
