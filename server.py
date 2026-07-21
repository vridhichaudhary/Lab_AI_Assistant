"""
server.py — Flask backend for the IOCL Laboratory Results Assistant.
Serves the HTML frontend and exposes REST API endpoints.

Ingestion paths:
  • .htm/.html  → lab_parser (structured nobr-cell extraction) → lab_ingest (SQLite)
  • other types → lab_assistant.parsers (generic pandas-based) → lab_assistant.db

Query routing:
  1. lab_query (exact structured lookup, zero LLM tokens) — if a known sample matches
  2. lab_assistant.chat / LLM fallback — for everything else
"""
import os
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, jsonify, render_template
from lab_assistant.db import (init_db, run_cleanup, get_all_reports,
                               delete_report, insert_report,
                               insert_lab_results, get_conn)
from lab_assistant.parsers import parse_file
from lab_assistant.chat import answer as lab_answer

# ── New structured lab pipeline ───────────────────────────────────────────────
from lab_ingest import ingest_lab_reports, load_records_from_db, init_lab_table
from lab_query import query_records, format_records_as_tables

LAB_DB_PATH = Path("data/lab_results_structured.db")
LAB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
init_lab_table(LAB_DB_PATH)

app = Flask(__name__, template_folder="templates", static_folder="static")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
MODEL_WATERFALL = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
UPLOADS_DIR = Path("data/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Initialise DB and clean expired files on startup
init_db()
_cleaned = run_cleanup()


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    reports = get_all_reports()
    total_records = sum(r.get("result_count", 0) for r in reports)
    morning = sum(1 for r in reports if r.get("shift") == "M")
    evening = sum(1 for r in reports if r.get("shift") == "E")

    conn = get_conn()
    samples = conn.execute(
        "SELECT COUNT(DISTINCT sample_name) FROM lab_results"
    ).fetchone()[0]
    conn.close()

    return jsonify({
        "total_reports":   len(reports),
        "total_records":   total_records,
        "morning_count":   morning,
        "evening_count":   evening,
        "unique_samples":  samples,
        "recent_reports":  reports[:8],
        "cleaned_on_boot": _cleaned,
    })


@app.route("/api/reports")
def api_reports():
    return jsonify(get_all_reports())


@app.route("/api/reports/<report_id>", methods=["DELETE"])
def api_delete_report(report_id):
    delete_report(report_id)
    return jsonify({"success": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    file = request.files.get("file")
    report_date_str = request.form.get("report_date", str(date.today()))
    uploaded_by     = request.form.get("uploaded_by", "Unknown").strip() or "Unknown"

    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = Path(file.filename).suffix.lower()

    # ── HTM / HTML → new structured parser ───────────────────────────────────
    if ext in (".htm", ".html"):
        try:
            import tempfile, os as _os
            file_bytes = file.read()
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_"
                                for c in file.filename)
            tmp_path = UPLOADS_DIR / safe_name
            tmp_path.write_bytes(file_bytes)

            summary = ingest_lab_reports([tmp_path], LAB_DB_PATH)
            num_rows  = summary["num_rows_parsed"]
            num_vals  = summary["num_parameter_values_inserted"]
            shifts    = list({r.shift for r in load_records_from_db(LAB_DB_PATH)
                              if r.source_file == safe_name}) or ["M/E/N"]
            dates     = summary["date_range"]

            if num_rows == 0:
                return jsonify({
                    "error": "No data rows extracted. "
                             "Please verify this is a valid PX/PTA lab report."
                }), 400

            return jsonify({
                "success":           True,
                "report_id":         safe_name,
                "records_extracted": num_vals,
                "detected_date":     dates[0] if dates else report_date_str,
                "detected_shift":    "/".join(shifts),
                "file_name":         file.filename,
                "parser":            "structured-htm",
                "unique_samples":    summary["unique_samples"],
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            return jsonify({"error": f"Failed to parse HTM file: {e}"}), 500

    # ── Other types → generic pandas parser → lab_assistant DB ───────────────
    try:
        file_bytes = file.read()
        rows, meta = parse_file(file_bytes, file.filename)
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {e}"}), 500

    report_date = meta.get("report_date") or report_date_str
    shift       = meta.get("shift") or "Unknown"

    if not rows:
        return jsonify({
            "error": "No data rows extracted. "
                     "Please verify the file contains a data table."
        }), 400

    safe_name = "".join(c if c.isalnum() or c in "._-" else "_"
                        for c in file.filename)
    file_path = UPLOADS_DIR / f"{report_date}_{shift}_{safe_name}"
    file_path.write_bytes(file_bytes)

    report_id = insert_report(
        report_date=report_date,
        shift=shift,
        uploaded_by=uploaded_by,
        original_file_name=file.filename,
        file_path=str(file_path),
    )
    insert_lab_results(report_id, rows)

    return jsonify({
        "success":           True,
        "report_id":         report_id,
        "records_extracted": len(rows),
        "detected_date":     report_date,
        "detected_shift":    "Evening" if shift == "E" else
                             "Morning" if shift == "M" else shift,
        "file_name":         file.filename,
        "parser":            "generic",
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    # ── Route 1: Structured lab_query (zero LLM, exact precision) ────────────
    # Load all structured records from the HTM parser DB and check if
    # the question mentions any known sample name.
    try:
        lab_records = load_records_from_db(LAB_DB_PATH)
        if lab_records:
            known_samples = {r.sample for r in lab_records}
            q_low = question.lower()
            if any(s.lower() in q_low for s in known_samples):
                results, parsed, warnings = query_records(question, lab_records)
                if results:
                    return jsonify({"response": format_records_as_tables(results)})
                if warnings:
                    return jsonify({"response": "\n".join(warnings)})
    except Exception:
        pass  # if structured path fails, fall through to LLM

    # ── Route 2: LLM fallback for generic / non-sample questions ─────────────
    if not GOOGLE_API_KEY:
        return jsonify({"error": "Google API key not configured on the server."}), 500

    for model in MODEL_WATERFALL:
        try:
            response = lab_answer(question, api_key=GOOGLE_API_KEY, model=model)
            return jsonify({"response": response})
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                continue   # try next model
            return jsonify({"error": err}), 500

    return jsonify({
        "error": "All AI models are temporarily rate-limited. "
                 "Please wait a moment and try again."
    }), 429


if __name__ == "__main__":
    app.run(debug=True, port=5001, host="0.0.0.0")
