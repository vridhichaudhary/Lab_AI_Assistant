"""
Industry RAG Chatbot — Consumer-friendly edition.
- Upload documents (PDF, TXT, DOCX, CSV)
- See all indexed documents and delete any of them
- Chat with your knowledge base powered by Google Gemini
"""

import warnings
warnings.filterwarnings("ignore")

import os, glob, json, shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Works locally; on Streamlit Cloud, secrets are set via the dashboard

# ── LangChain / Google ────────────────────────────────────────────
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import HumanMessage, AIMessage
from langchain_community.document_loaders import (
    PyPDFLoader, TextLoader, CSVLoader, Docx2txtLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_community.chat_message_histories import ChatMessageHistory

import streamlit as st

# ── Paths ─────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
TMP_DIR         = BASE_DIR / "data" / "tmp"
KB_DIR          = BASE_DIR / "data" / "knowledge_base"   # single persistent vectorstore
REGISTRY_FILE   = BASE_DIR / "data" / "docs_registry.json"

TMP_DIR.mkdir(parents=True, exist_ok=True)
KB_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ────────────────────────────────────────────────────────
# Read from Streamlit Cloud secrets first, then fall back to local .env
import streamlit as _st
try:
    GOOGLE_API_KEY = _st.secrets["GOOGLE_API_KEY"]
except Exception:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL    = "gemini-2.5-flash"
EMBEDDING_MODEL = "models/gemini-embedding-2"

WELCOME_MSG     = "Hello! How can I help you today? Ask me anything about your documents."

ANSWER_PROMPT = PromptTemplate.from_template("""\
You are a helpful, professional assistant. Answer the user's question using ONLY \
the context provided below. If the answer is not in the context, say so clearly. \
Always respond in clear, simple English.

Context from documents:
{context}

Conversation so far:
{history}

User: {question}
Assistant:""")

CONDENSE_PROMPT = PromptTemplate.from_template("""\
Given the conversation history and a follow-up question, rephrase the follow-up \
as a standalone question that makes sense on its own.

History:
{history}

Follow-up: {question}
Standalone question:""")

# ── Registry helpers (track which docs are indexed) ───────────────

def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            pass
    return {}          # {filename: {"chunks": n, "added": "ISO date"}}


def save_registry(reg: dict):
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


# ── Embedding / vectorstore helpers ───────────────────────────────

def get_embeddings():
    return GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=GOOGLE_API_KEY,
    )


def load_vectorstore():
    """Load (or create) the persistent Chroma vectorstore."""
    emb = get_embeddings()
    return Chroma(
        embedding_function=emb,
        persist_directory=str(KB_DIR),
    )


def index_file(uploaded_file) -> int:
    """Save uploaded file to tmp, load, split, embed, persist. Returns chunk count."""
    tmp_path = TMP_DIR / uploaded_file.name
    tmp_path.write_bytes(uploaded_file.read())

    ext = uploaded_file.name.lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        loader = PyPDFLoader(str(tmp_path))
    elif ext == "txt":
        loader = TextLoader(str(tmp_path), encoding="utf-8")
    elif ext == "csv":
        loader = CSVLoader(str(tmp_path), encoding="utf-8")
    elif ext in ("docx", "doc"):
        loader = Docx2txtLoader(str(tmp_path))
    elif ext in ("html", "htm"):
        from langchain_community.document_loaders import BSHTMLLoader
        loader = BSHTMLLoader(str(tmp_path))
    elif ext == "xlsx":
        class CustomExcelLoader:
            def __init__(self, path):
                self.path = path
            def load(self):
                import pandas as pd
                from langchain_core.documents import Document
                df = pd.read_excel(self.path)
                docs = []
                for i, row in df.iterrows():
                    content = " | ".join(f"{k}: {v}" for k, v in row.items() if pd.notna(v))
                    docs.append(Document(page_content=content, metadata={"source": self.path, "row": i}))
                return docs
        loader = CustomExcelLoader(str(tmp_path))
    else:
        raise ValueError(f"Unsupported file type: .{ext}")

    docs = loader.load()
    # Attach source filename to every chunk for later deletion
    for d in docs:
        d.metadata["source_filename"] = uploaded_file.name

    splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)
    chunks = splitter.split_documents(docs)

    import time
    vs = load_vectorstore()
    
    # Batch chunks to respect Google API free-tier rate limits (100 RPM)
    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        success = False
        retries = 0
        while not success and retries < 4:
            try:
                vs.add_documents(batch)
                success = True
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(25) # Wait for quota to refresh
                    retries += 1
                else:
                    raise e
        if not success:
            raise Exception("Failed to embed document after multiple retries due to rate limits.")

    tmp_path.unlink(missing_ok=True)
    return len(chunks)


def delete_document(filename: str):
    """Remove all chunks belonging to a document from the vectorstore."""
    vs = load_vectorstore()
    # Get IDs of all chunks with this source_filename
    results = vs.get(where={"source_filename": filename})
    ids = results.get("ids", [])
    if ids:
        vs.delete(ids=ids)
    return len(ids)


# ── LLM chain helpers ─────────────────────────────────────────────

def get_history_text(history: ChatMessageHistory) -> str:
    lines = []
    for m in history.messages[-8:]:   # keep last 4 exchanges
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        lines.append(f"{role}: {m.content}")
    return "\n".join(lines)


def answer_question(question: str, history: ChatMessageHistory) -> tuple[str, list]:
    """Condense question → retrieve docs → answer. Returns (answer, source_docs)."""
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=GOOGLE_API_KEY,
        temperature=0.3,
    )
    condense_llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=GOOGLE_API_KEY,
        temperature=0.0,
    )

    history_text = get_history_text(history)

    # Step 1 — condense to standalone question
    if history_text:
        standalone = condense_llm.invoke(
            CONDENSE_PROMPT.format(history=history_text, question=question)
        ).content.strip()
    else:
        standalone = question

    # Step 2 — retrieve relevant chunks
    vs = load_vectorstore()
    retriever = vs.as_retriever(search_type="similarity", search_kwargs={"k": 8})
    docs = retriever.invoke(standalone)
    context = "\n\n".join(d.page_content for d in docs)

    # Step 3 — generate answer
    answer = llm.invoke(
        ANSWER_PROMPT.format(
            context=context,
            history=history_text,
            question=question,
        )
    ).content.strip()

    return answer, docs


# ── Session state bootstrap ───────────────────────────────────────

def init_session():
    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content": WELCOME_MSG}]
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = ChatMessageHistory()
    if "registry" not in st.session_state:
        st.session_state.registry = load_registry()


# ── UI ────────────────────────────────────────────────────────────

def render_sidebar():
    with st.sidebar:
        st.markdown("## 📚 Knowledge Base")

        # ── Upload section ─────────────────────────────────────────
        st.markdown("### Add Documents")
        uploaded = st.file_uploader(
            "Upload a file",
            type=["pdf", "txt", "csv", "docx", "html", "xlsx"],
            accept_multiple_files=True,
            label_visibility="collapsed"
        )

        if st.button("📥 Add to Knowledge Base", use_container_width=True,
                     disabled=not uploaded):
            reg = st.session_state.registry
            progress = st.progress(0, text="Processing…")
            errors = []
            for i, f in enumerate(uploaded):
                progress.progress((i) / len(uploaded), text=f"Indexing {f.name}…")
                if f.name in reg:
                    st.warning(f"**{f.name}** is already in the knowledge base. Delete it first to re-upload.")
                    continue
                try:
                    n_chunks = index_file(f)
                    from datetime import datetime
                    reg[f.name] = {"chunks": n_chunks, "added": datetime.now().strftime("%Y-%m-%d")}
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
            save_registry(reg)
            st.session_state.registry = reg
            progress.progress(1.0, text="Done!")
            if errors:
                for e in errors:
                    st.error(e)
            else:
                # If everything succeeded, rerun immediately to update the list
                st.rerun()

        st.divider()

        # ── Documents list ─────────────────────────────────────────
        reg = st.session_state.registry
        if not reg:
            st.info("No documents yet. Upload some above.")
        else:
            st.markdown(f"### Stored Documents ({len(reg)})")
            for fname, meta in list(reg.items()):
                col_name, col_del = st.columns([4, 1])
                ext = fname.rsplit(".", 1)[-1].upper()
                icon = {"PDF": "📄", "TXT": "📝", "DOCX": "📃", "CSV": "📊"}.get(ext, "📁")
                col_name.markdown(
                    f"{icon} **{fname}**  \n"
                    f"<small>{meta['chunks']} chunks · {meta['added']}</small>",
                    unsafe_allow_html=True,
                )
                if col_del.button("🗑️", key=f"del_{fname}", help=f"Delete {fname}"):
                    with st.spinner(f"Removing {fname}…"):
                        try:
                            delete_document(fname)
                            del st.session_state.registry[fname]
                            save_registry(st.session_state.registry)
                            st.success(f"**{fname}** removed.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error removing {fname}: {e}")

        st.divider()
        # API key status
        if GOOGLE_API_KEY:
            st.success("✅ Google API key loaded", icon="🔑")
        else:
            st.error("❌ No Google API key found in .env")


def render_chat():
    st.title("🤖 AI Document Assistant")
    st.caption("Ask questions about your uploaded documents — powered by Google Gemini")

    col1, col2 = st.columns([6, 1])
    with col2:
        if st.button("🗑️ Clear chat"):
            st.session_state.messages = [{"role": "assistant", "content": WELCOME_MSG}]
            st.session_state.chat_history = ChatMessageHistory()
            st.rerun()

    # Render message history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    if prompt := st.chat_input("Ask a question about your documents…"):
        if not GOOGLE_API_KEY:
            st.error("Please add your Google API key to the `.env` file and restart.")
            st.stop()

        if not st.session_state.registry:
            st.warning("Please upload at least one document in the sidebar first.")
            st.stop()

        # Show user message immediately
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Get and stream assistant response
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    answer, source_docs = answer_question(
                        prompt, st.session_state.chat_history
                    )

                    st.markdown(answer)

                    # Source accordion
                    if source_docs:
                        with st.expander("📄 Sources used", expanded=False):
                            seen = set()
                            for doc in source_docs:
                                src = doc.metadata.get("source_filename",
                                       doc.metadata.get("source", "unknown"))
                                page = doc.metadata.get("page", "")
                                label = src + (f" · page {page+1}" if page != "" else "")
                                if label not in seen:
                                    st.markdown(f"- **{label}**")
                                    seen.add(label)

                    # Update history
                    st.session_state.chat_history.add_user_message(prompt)
                    st.session_state.chat_history.add_ai_message(answer)
                    st.session_state.messages.append({"role": "assistant", "content": answer})

                except Exception as e:
                    err = str(e)
                    st.error(f"⚠️ {err}")
                    st.session_state.messages.append(
                        {"role": "assistant", "content": f"Sorry, I ran into an error: {err}"}
                    )


# ── Entry point ───────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="AI Document Assistant",
        page_icon="🤖",
        layout="wide",
    )
    init_session()
    render_sidebar()
    render_chat()


if __name__ == "__main__":
    main()
