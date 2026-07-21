"""
lab_app_demo.py
================
Standalone Streamlit demo for the lab-report query module. This is
deliberately separate from the main RAG chatbot's app.py so you can test
and validate the lab-query behaviour in isolation before wiring it into
the bigger project (see ANTIGRAVITY_INTEGRATION.md for how to merge it in).

Run with:
    streamlit run lab_app_demo.py
"""

from __future__ import annotations

import streamlit as st

from lab_ingest import ingest_lab_reports, load_records_from_db
from lab_query import query_records, format_records_as_tables

DB_PATH = "lab_demo.db"

st.set_page_config(page_title="Lab Results Query (Demo)", page_icon="🧪", layout="wide")
st.title("🧪 PX/PTA Lab Results — Structured Query Demo")
st.caption(
    "Upload one or more lab-result .htm reports, then ask questions like "
    "**\"Benzene Product 7 July M\"** or **\"DSN 5 July\"** (omit the shift "
    "letter to get Morning + Evening + Night together)."
)

st.sidebar.header("Upload lab report(s)")
uploaded = st.sidebar.file_uploader(
    "PX/PTA lab result .htm file(s)", type=["htm", "html"], accept_multiple_files=True
)

if uploaded and st.sidebar.button("Parse & Load", use_container_width=True):
    import tempfile, os

    tmp_paths = []
    with st.sidebar.status("Parsing uploaded reports...", expanded=True) as status:
        for f in uploaded:
            tmp_path = os.path.join(tempfile.gettempdir(), f.name)
            with open(tmp_path, "wb") as out:
                out.write(f.getvalue())
            tmp_paths.append(tmp_path)
        summary = ingest_lab_reports(tmp_paths, DB_PATH)
        status.update(
            label=(
                f"✅ Parsed {summary['num_rows_parsed']} sample/date/shift rows, "
                f"{summary['num_parameter_values_inserted']} parameter values, "
                f"{summary['unique_samples']} unique samples across "
                f"{summary['unique_materials']} materials. "
                f"Dates found: {', '.join(summary['date_range'])}."
            ),
            state="complete",
        )
    st.session_state["lab_loaded"] = True

st.divider()

if not st.session_state.get("lab_loaded"):
    st.info("👈 Upload and parse a lab report .htm file to get started.")
else:
    query = st.text_input(
        "Ask about a sample",
        placeholder='e.g. "Benzene Product 7 July M" or "DSN 5 July"',
    )
    if query:
        records = load_records_from_db(DB_PATH)
        results, parsed, warnings = query_records(query, records)

        with st.expander("🔍 How this query was interpreted"):
            st.write(
                {
                    "sample_hint": parsed.sample_hint,
                    "day": parsed.day,
                    "month": parsed.month,
                    "year": parsed.year,
                    "shift": parsed.shift,
                }
            )

        for w in warnings:
            st.warning(w)

        if results:
            for r in results:
                shift_name = {"M": "Morning", "E": "Evening", "N": "Night"}.get(r.shift, r.shift)
                st.subheader(f"{r.sample} — {r.date} — {shift_name} shift")
                st.caption(f"{r.material} · source: {r.source_file}")
                if r.values:
                    st.table(
                        [
                            {"Parameter": p, "Unit": u, "Value": v}
                            for p, (u, v) in r.values.items()
                        ]
                    )
                else:
                    st.write("_No parameters were reported for this row._")
