"""
lab_app.py — IOCL Laboratory Results Assistant
Four tabs: Dashboard | Upload Reports | Chat | Search History
"""
import os
import warnings
import time
from datetime import date, datetime
from pathlib import Path

warnings.filterwarnings("ignore")

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

# ── Secrets ──────────────────────────────────────────────────────────────────
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Models — best available given free tier limits
MODEL_WATERFALL = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"]

# ── Project setup ─────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
UPLOADS_DIR  = BASE_DIR / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── Initialise DB ─────────────────────────────────────────────────────────────
from lab_assistant.db import init_db, run_cleanup, get_all_reports, \
    delete_report, insert_report, insert_lab_results
from lab_assistant.parsers import parse_file, detect_metadata
from lab_assistant.chat import answer as lab_answer

init_db()

# Run 7-day cleanup on every startup (lightweight check)
_cleaned = run_cleanup()


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_text(msg) -> str:
    """Handle both string and list-of-dict Gemini responses."""
    if not msg:
        return ""
    content = msg.content if hasattr(msg, "content") else msg
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = [p["text"] if isinstance(p, dict) else str(p) for p in content]
        return "".join(texts).strip()
    return str(content).strip()


def get_working_model() -> str:
    """Quick test to find a model that currently responds."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    for m in MODEL_WATERFALL:
        try:
            llm = ChatGoogleGenerativeAI(model=m, google_api_key=GOOGLE_API_KEY,
                                         temperature=0)
            llm.invoke("ping")
            return m
        except Exception:
            time.sleep(2)
    return MODEL_WATERFALL[-1]


# ── Session state bootstrap ───────────────────────────────────────────────────

def _init_session():
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = [
            {"role": "assistant",
             "content": "👋 Hello! I'm your IOCL Lab Assistant. "
                        "Ask me things like:\n"
                        "- *Show today's Morning shift results*\n"
                        "- *Give me Evening shift Reformate data*\n"
                        "- *What is the Density of NHT Feed?*"}
        ]
    if "chat_history_log" not in st.session_state:
        st.session_state.chat_history_log = []   # [(question, answer, timestamp)]
    if "active_model" not in st.session_state:
        st.session_state.active_model = MODEL_WATERFALL[0]


# ── UI ────────────────────────────────────────────────────────────────────────

def render_dashboard():
    st.header("📊 Dashboard")

    reports = get_all_reports()

    col1, col2, col3 = st.columns(3)
    col1.metric("📄 Total Reports", len(reports))
    col2.metric("🧪 Total Records",
                sum(r.get("result_count", 0) for r in reports))
    morning = sum(1 for r in reports if r.get("shift") == "M")
    col3.metric("🌅 Morning / 🌆 Evening", f"{morning} / {len(reports)-morning}")

    st.divider()

    if not reports:
        st.info("No reports uploaded yet. Go to **Upload Reports** to get started.")
        return

    st.subheader("📋 Uploaded Reports")
    for r in reports:
        with st.container(border=True):
            c1, c2 = st.columns([5, 1])
            with c1:
                shift_label = "🌅 Morning" if r.get("shift") == "M" else "🌆 Evening"
                st.markdown(
                    f"**{r.get('original_file_name', 'Unknown')}**  \n"
                    f"📅 Report Date: `{r.get('report_date')}` &nbsp;|&nbsp; "
                    f"{shift_label} &nbsp;|&nbsp; "
                    f"🧪 `{r.get('result_count', 0)}` records  \n"
                    f"👤 Uploaded by: *{r.get('uploaded_by', '—')}*  \n"
                    f"⏳ Expires: `{r.get('expires_at', '—')[:10]}`"
                )
            with c2:
                if st.button("🗑️ Delete", key=f"del_{r['report_id']}"):
                    delete_report(r["report_id"])
                    st.rerun()


def render_upload():
    st.header("📤 Upload Laboratory Report")

    uploaded = st.file_uploader(
        "Choose a lab report file",
        type=["html", "htm", "xlsx", "xls", "csv", "pdf"],
        accept_multiple_files=False,
        label_visibility="collapsed"
    )

    # Auto-detect metadata from file
    auto_date = str(date.today())
    auto_shift = None

    if uploaded:
        try:
            raw = uploaded.read()
            uploaded.seek(0)
            detected = detect_metadata(uploaded.name,
                                       raw.decode("utf-8", errors="ignore"))
            auto_date  = detected.get("report_date") or auto_date
            auto_shift = detected.get("shift")
        except Exception:
            pass

    st.markdown("### Report Metadata")
    col1, col2 = st.columns(2)

    with col1:
        report_date_input = st.date_input(
            "Report Date",
            value=datetime.fromisoformat(auto_date).date() if auto_date else date.today()
        )
        shift_input = st.radio(
            "Shift",
            ["Morning (M)", "Evening (E)"],
            index=0 if auto_shift != "E" else 1,
            horizontal=True
        )

    with col2:
        uploaded_by = st.text_input("Uploaded By", placeholder="Your name or department")

    st.divider()

    btn_disabled = uploaded is None
    if st.button("📥 Parse & Save to Database", use_container_width=True,
                 disabled=btn_disabled, type="primary"):
        if not uploaded:
            st.warning("Please choose a file first.")
            return

        shift_code = "M" if "Morning" in shift_input else "E"
        report_date_str = str(report_date_input)

        with st.spinner("Parsing file…"):
            try:
                raw = uploaded.read()
                rows, _ = parse_file(raw, uploaded.name)
            except Exception as e:
                st.error(f"❌ Failed to parse file: {e}")
                return

        if not rows:
            st.warning("⚠️ No data rows were extracted from this file. "
                       "Please check the file format.")
            return

        # Save original file
        safe_name = uploaded.name.replace(" ", "_")
        file_path = UPLOADS_DIR / f"{report_date_str}_{shift_code}_{safe_name}"
        file_path.write_bytes(raw)

        # Insert into DB
        report_id = insert_report(
            report_date      = report_date_str,
            shift            = shift_code,
            uploaded_by      = uploaded_by or "Unknown",
            original_file_name = uploaded.name,
            file_path        = str(file_path),
        )
        insert_lab_results(report_id, rows)

        st.success(
            f"✅ **{uploaded.name}** uploaded successfully!  \n"
            f"📊 Extracted **{len(rows)}** data records.  \n"
            f"⏳ Will auto-delete after 7 days."
        )
        st.balloons()


def render_chat():
    st.header("🤖 Lab Data Assistant")
    st.caption("Ask questions about uploaded laboratory reports. Only real data from the database is used.")

    if not GOOGLE_API_KEY:
        st.error("⚠️ Google API key is missing. Please set it in your `.env` file.")
        return

    # Display conversation
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input
    prompt = st.chat_input("Ask about lab results… e.g. 'Show today's Morning shift'")
    if not prompt:
        return

    # Show user message
    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Generate answer
    with st.chat_message("assistant"):
        with st.spinner("Querying lab database…"):
            try:
                response = lab_answer(
                    prompt,
                    api_key=GOOGLE_API_KEY,
                    model=st.session_state.active_model
                )
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    # try fallback model
                    models = MODEL_WATERFALL[:]
                    if st.session_state.active_model in models:
                        models.remove(st.session_state.active_model)
                    if models:
                        st.session_state.active_model = models[0]
                        try:
                            response = lab_answer(prompt, api_key=GOOGLE_API_KEY,
                                                  model=st.session_state.active_model)
                        except Exception as e2:
                            response = f"⚠️ Service temporarily unavailable: {e2}"
                    else:
                        response = "⚠️ All AI models are rate-limited. Please wait a minute."
                else:
                    response = f"⚠️ Error: {err}"

        st.markdown(response)

    # Persist
    st.session_state.chat_messages.append({"role": "assistant", "content": response})
    st.session_state.chat_history_log.append(
        (prompt, response, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )


def render_history():
    st.header("📜 Search History")

    if not st.session_state.chat_history_log:
        st.info("No queries yet. Ask something in the **Chat** tab.")
        return

    if st.button("🗑️ Clear History"):
        st.session_state.chat_history_log.clear()
        st.rerun()

    for i, (q, a, ts) in enumerate(reversed(st.session_state.chat_history_log)):
        with st.expander(f"🕐 {ts}  —  {q[:80]}{'…' if len(q)>80 else ''}",
                         expanded=(i == 0)):
            st.markdown(f"**You:** {q}")
            st.divider()
            st.markdown(f"**Assistant:** {a}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="IOCL Lab Assistant",
        page_icon="🔬",
        layout="wide",
    )

    _init_session()

    # Sidebar navigation
    with st.sidebar:
        st.image(
            "https://upload.wikimedia.org/wikipedia/en/thumb/e/e6/Indian-oil.svg/"
            "200px-Indian-oil.svg.png",
            width=100
        )
        st.title("🔬 IOCL Lab Assistant")
        st.caption("Internal Laboratory Data System")
        st.divider()

        page = st.radio(
            "Navigate",
            ["📊 Dashboard", "📤 Upload Reports", "🤖 Chat", "📜 Search History"],
            label_visibility="collapsed"
        )

        st.divider()

        # Cleanup indicator
        if _cleaned:
            st.success(f"🗑️ Auto-cleaned {_cleaned} expired report(s) on startup.")

        # API key status
        if GOOGLE_API_KEY:
            st.success("🔑 API key loaded")
        else:
            st.error("🔑 API key missing")

    # Route pages
    if page == "📊 Dashboard":
        render_dashboard()
    elif page == "📤 Upload Reports":
        render_upload()
    elif page == "🤖 Chat":
        render_chat()
    elif page == "📜 Search History":
        render_history()


if __name__ == "__main__":
    main()
