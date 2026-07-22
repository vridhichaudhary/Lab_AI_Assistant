"""
server.py — Flask backend for the IOCL Laboratory Results Assistant.
Serves the HTML frontend and exposes REST API endpoints.

Storage strategy (cloud-persistent):
  • .htm/.html  → lab_parser (structured nobr-cell extraction)
                → lab_ingest  (PostgreSQL + Supabase Storage)
  • other types → lab_assistant.parsers (generic pandas-based)
                → lab_assistant.db (PostgreSQL)

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
from supabase import create_client

# ── New structured lab pipeline ───────────────────────────────────────────────
from lab_ingest import (ingest_lab_reports, load_records_from_db, init_lab_table,
                        cleanup_structured_reports, get_all_structured_reports,
                        delete_structured_report)
from lab_query import query_records, format_records_as_tables

SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY", "")
STORAGE_BUCKET = "lab-reports"

init_lab_table()

app = Flask(__name__, template_folder="templates", static_folder="static")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
MODEL_WATERFALL = ["gemini-2.0-flash", "gemini-1.5-flash"]

# Initialise DB and clean expired files on startup
init_db()
_cleaned  = run_cleanup()
_cleaned += cleanup_structured_reports()


def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    reports      = get_all_reports()
    total_records = sum(r.get("result_count", 0) for r in reports)
    morning = sum(1 for r in reports if r.get("shift") == "M")
    evening = sum(1 for r in reports if r.get("shift") == "E")

    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT sample_name) FROM lab_results")
    samples = cur.fetchone()[0]
    cur.close()
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


@app.route("/api/structured-reports")
def api_structured_reports():
    """Returns all stored HTM lab reports with date ranges and expiry info."""
    return jsonify(get_all_structured_reports())


@app.route("/api/structured-reports/<path:report_id>", methods=["DELETE"])
def api_delete_structured_report(report_id):
    delete_structured_report(report_id)
    return jsonify({"success": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    file            = request.files.get("file")
    report_date_str = request.form.get("report_date", str(date.today()))
    uploaded_by     = request.form.get("uploaded_by", "Unknown").strip() or "Unknown"

    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext        = Path(file.filename).suffix.lower()
    file_bytes = file.read()

    # ── HTM / HTML → new structured parser ───────────────────────────────────
    if ext in (".htm", ".html"):
        import tempfile, io
        try:
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_"
                                for c in file.filename)

            # Write to a temp file so lab_parser can read it (it expects a path)
            with tempfile.NamedTemporaryFile(suffix=".htm", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = Path(tmp.name)

            # Pass temp path + raw bytes so ingest can upload to Supabase Storage
            summary  = ingest_lab_reports([tmp_path], file_bytes=file_bytes)
            tmp_path.unlink(missing_ok=True)   # clean up temp

            num_rows = summary["num_rows_parsed"]
            num_vals = summary["num_parameter_values_inserted"]

            if num_rows == 0:
                return jsonify({
                    "error": "No data rows extracted. "
                             "Please verify this is a valid PX/PTA lab report."
                }), 400

            all_records = load_records_from_db()
            shifts = list({r.shift for r in all_records
                           if r.source_file == safe_name}) or ["M/E/N"]
            dates  = summary["date_range"]

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

    # Upload to Supabase Storage
    safe_name    = "".join(c if c.isalnum() or c in "._-" else "_"
                           for c in file.filename)
    storage_path = f"{report_date}_{shift}_{safe_name}"
    try:
        sb = _supabase()
        sb.storage.from_(STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"upsert": "true"},
        )
    except Exception as e:
        print(f"[warn] Storage upload failed: {e}")
        storage_path = ""

    report_id = insert_report(
        report_date=report_date,
        shift=shift,
        uploaded_by=uploaded_by,
        original_file_name=file.filename,
        file_path=storage_path,
        storage_path=storage_path,
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

    # ── Route 1: Structured lab_query (fuzzy, zero LLM tokens) ───────────────
    # Always try the structured path first — it uses tokenized fuzzy matching
    # so informal queries like "pta 7 july" or "601 btm" are handled correctly
    # WITHOUT requiring the full sample name to appear as an exact substring.
    try:
        lab_records = load_records_from_db()
        if lab_records:
            results, parsed, warnings = query_records(question, lab_records)
            if results:
                return jsonify({"response": format_records_as_tables(results)})
            # A sample was found in the hint but no matching rows for date/shift
            if warnings and parsed.sample_hint:
                return jsonify({"response": "\n".join(warnings)})
            # Structured path found nothing — fall through to LLM for general Qs
    except Exception as exc:
        print(f"[warn] Structured query error: {exc}")

    # ── Route 2: LLM fallback for non-lab / general questions ────────────────
    if not GOOGLE_API_KEY:
        return jsonify({
            "response": (
                "I couldn't find any lab records matching your query. "
                "Please upload a lab report first, or rephrase your sample name."
            )
        })

    for model in MODEL_WATERFALL:
        try:
            response = lab_answer(question, api_key=GOOGLE_API_KEY, model=model)
            return jsonify({"response": response})
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                continue
            import traceback
            return jsonify({"error": err, "traceback": traceback.format_exc()}), 500

    return jsonify({
        "error": "All AI models are temporarily rate-limited. "
                 "Please wait a moment and try again."
    }), 429


if __name__ == "__main__":
    app.run(debug=True, port=5001, host="0.0.0.0")
