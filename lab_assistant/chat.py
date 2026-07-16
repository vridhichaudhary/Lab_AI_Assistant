"""
chat.py — Natural language query handler for the IOCL Lab Assistant.

Strategy:
  1. Fast rule-based extraction of date / shift / sample / parameter.
  2. Direct SQL query — no LLM tokens wasted for simple lookups.
  3. LLM fallback only for complex or ambiguous queries.
"""
import re
from datetime import date, timedelta

from lab_assistant.db import query_results, get_conn, DB_PATH


# ── Natural language → structured filters ────────────────────────────────────

_SHIFT_MAP = {
    "morning": "M", "m shift": "M", "morning shift": "M",
    "evening": "E", "e shift": "E", "evening shift": "E",
}

def _extract_shift(text: str) -> str | None:
    t = text.lower()
    for kw, code in _SHIFT_MAP.items():
        if kw in t:
            return code
    # standalone single letter at word boundary
    if re.search(r"\bm\b", t):
        return "M"
    if re.search(r"\be\b", t):
        return "E"
    return None


def _extract_date(text: str) -> str | None:
    """Return ISO date string if found, else None."""
    today = date.today()
    t = text.lower()

    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    if "last monday" in t:
        delta = (today.weekday() - 0) % 7 or 7
        return (today - timedelta(days=delta)).isoformat()

    # dd-mm-yyyy / dd.mm.yyyy / dd/mm/yyyy
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"

    # yyyy-mm-dd
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)

    # "5 July", "July 5"
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }
    mo_re = "|".join(months.keys())
    m = re.search(rf"(\d{{1,2}})\s+({mo_re})", t)
    if not m:
        m = re.search(rf"({mo_re})\s+(\d{{1,2}})", t)
        if m:
            mon_str, day_str = m.group(1), m.group(2)
        else:
            mon_str = day_str = None
    else:
        day_str, mon_str = m.group(1), m.group(2)

    if mon_str and day_str:
        mn = months[mon_str]
        return f"{today.year}-{mn:02d}-{int(day_str):02d}"

    return None


def _extract_parameter(text: str) -> str | None:
    """Detect if user is asking for a specific parameter only."""
    m = re.search(
        r"\b(density|sulfur|iron|px|bz|tol|mx|ox|h2|c1|c2|ibp|fbp|chloride|reformate)\b",
        text, re.I
    )
    if m:
        word = m.group(1).lower()
        if word == "reformate":
            return None
        return word
    return None


def _get_all_sample_names() -> list[str]:
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT DISTINCT sample_name FROM lab_results WHERE sample_name IS NOT NULL"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _fuzzy_sample_match(text: str) -> str | None:
    """Find the best matching sample name from DB using substring matching."""
    samples = _get_all_sample_names()
    t = text.lower()
    # Exact
    for s in samples:
        if s.lower() == t:
            return s
    # Substring
    for s in samples:
        if s.lower() in t or t in s.lower():
            return s
    # Word overlap
    words = set(t.split())
    best, best_score = None, 0
    for s in samples:
        score = sum(1 for w in s.lower().split() if w in words)
        if score > best_score:
            best_score = score
            best = s
    return best if best_score > 0 else None


# ── Result formatter ──────────────────────────────────────────────────────────

def _format_results(rows: list[dict]) -> str:
    """Format DB rows into a beautiful Markdown response."""
    if not rows:
        return "No matching laboratory record was found."

    from collections import defaultdict
    # Group: date -> shift -> sample -> [(param, val)]
    grouped: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for r in rows:
        grouped[r["report_date"]][r["shift"]][r["sample_name"]].append(
            (r["parameter_name"], r["parameter_value"])
        )

    lines = []
    for rdate, shifts in sorted(grouped.items(), reverse=True):
        for shift, samples in sorted(shifts.items()):
            shift_label = "Morning Shift" if shift == "M" else "Evening Shift" if shift == "E" else shift
            
            lines.append(f"### 📅 {rdate} | {shift_label}")
            lines.append("")
            
            for sample, params in sorted(samples.items()):
                lines.append(f"**Sample:** `{sample}`")
                lines.append("")
                lines.append("| Parameter | Value |")
                lines.append("|---|---|")
                for pname, pval in params:
                    lines.append(f"| {pname} | {pval} |")
                lines.append("")
                lines.append("---")
                lines.append("")

    return "\n".join(lines).strip()


# ── LLM Fallback (SQL Agent) ──────────────────────────────────────────────────

def _llm_answer(question: str, api_key: str, model: str) -> str:
    """Use LangChain SQL Agent for complex / ambiguous queries."""
    from datetime import date as _date
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_community.utilities import SQLDatabase
    from langchain_community.agent_toolkits import create_sql_agent

    today_str = _date.today().isoformat()

    llm = ChatGoogleGenerativeAI(model=model, google_api_key=api_key, temperature=0)
    db  = SQLDatabase.from_uri(f"sqlite:///{DB_PATH}")

    PREFIX = (
        f"Today's date is {today_str}. "
        "You are an IOCL laboratory assistant. "
        "Answer only from the lab_results table. "
        "The 'shift' column contains 'M' for Morning and 'E' for Evening. "
        "NEVER return rows where parameter_value is empty (they are never stored empty). "
        "Format the final answer using Markdown tables grouped by date, shift, and sample."
    )

    agent = create_sql_agent(llm, db=db, verbose=False, prefix=PREFIX)
    try:
        res = agent.invoke({"input": question})
        return res.get("output", "No matching laboratory record was found.")
    except Exception as e:
        return f"Could not complete query: {str(e)}"


# ── Main entry point ──────────────────────────────────────────────────────────

def answer(question: str, api_key: str, model: str) -> str:
    """
    Resolve user question:
    1. Rule-based fast path (no LLM tokens used).
    2. LLM fallback for complex queries.
    """
    shift     = _extract_shift(question)
    rep_date  = _extract_date(question)
    parameter = _extract_parameter(question)
    sample    = _fuzzy_sample_match(question)

    # Fast path: we have at least one filter
    if rep_date or shift or sample:
        rows = query_results(
            report_date      = rep_date,
            shift            = shift,
            sample_filter    = sample,
            parameter_filter = parameter,
        )
        if rows:
            return _format_results(rows)

    # LLM fallback
    return _llm_answer(question, api_key, model)
