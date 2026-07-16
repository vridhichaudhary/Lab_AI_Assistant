/* ─── IOCL Lab Assistant — Frontend Logic ──────────────────────────────────── */

/* ═══ State ═══════════════════════════════════════════════════════════════════ */
const state = {
  currentPage: 'chat',
  selectedFile: null,
  chatHistory: JSON.parse(localStorage.getItem('lab_history') || '[]'),
  sending: false,
};

/* ═══ Utilities ═══════════════════════════════════════════════════════════════ */
function $(id) { return document.getElementById(id); }

function showToast(msg, type = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  setTimeout(() => { t.className = 'toast'; }, 3500);
}

function formatDate(isoStr) {
  if (!isoStr) return '—';
  const [y, m, d] = isoStr.slice(0,10).split('-');
  return `${d}-${m}-${y}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ═══ Navigation ══════════════════════════════════════════════════════════════ */
function navigate(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

  const pageEl = $(`page-${page}`);
  const navEl  = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (pageEl) pageEl.classList.add('active');
  if (navEl)  navEl.classList.add('active');

  state.currentPage = page;
  if (page === 'history') renderHistory();
}

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', e => {
    e.preventDefault();
    navigate(item.dataset.page);
  });
});

/* ═══ Status Check ════════════════════════════════════════════════════════════ */
async function checkStatus() {
  try {
    await fetch('/api/stats');
    updateApiStatus(true);
  } catch {
    updateApiStatus(false);
  }
}

/* ═══ Upload ══════════════════════════════════════════════════════════════════ */

// Set today's date as default
const today = new Date().toISOString().slice(0, 10);
if ($('report-date')) $('report-date').value = today;

const dropzone = $('dropzone');
const fileInput = $('file-input');

if (dropzone) {
  dropzone.addEventListener('click', () => fileInput.click());

  dropzone.addEventListener('dragover', e => {
    e.preventDefault();
    dropzone.classList.add('drag-over');
  });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
  dropzone.addEventListener('drop', e => {
    e.preventDefault();
    dropzone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) handleFileSelected(file);
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) handleFileSelected(fileInput.files[0]);
  });
}

function handleFileSelected(file) {
  state.selectedFile = file;
  $('file-selected').classList.add('show');
  $('file-selected-name').textContent = file.name;
  $('file-selected-size').textContent = formatBytes(file.size);
  $('upload-btn').disabled = false;

  const dateMatch = file.name.match(/(\d{2})[.\-/](\d{2})[.\-/](\d{4})/);
  if (dateMatch) {
    const [, dd, mm, yyyy] = dateMatch;
    $('report-date').value = `${yyyy}-${mm}-${dd}`;
  }

  const res = $('upload-result');
  res.className = 'upload-result';
}

function formatBytes(b) {
  if (b < 1024)       return `${b} B`;
  if (b < 1048576)    return `${(b/1024).toFixed(1)} KB`;
  return `${(b/1048576).toFixed(1)} MB`;
}

if ($('upload-btn')) {
  $('upload-btn').addEventListener('click', async () => {
    if (!state.selectedFile) return;

    $('upload-btn').disabled = true;
    $('upload-btn-text').innerHTML = '<i data-feather="loader" class="spin"></i> Parsing…';
    feather.replace();

    const form = new FormData();
    form.append('file', state.selectedFile);
    form.append('report_date', $('report-date').value);
    form.append('uploaded_by', $('uploaded-by').value || 'Unknown');

    try {
      const res  = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await res.json();
      const box  = $('upload-result');

      if (data.success) {
        box.className = 'upload-result success';
        $('upload-result-title').textContent = `✅ ${data.file_name} uploaded successfully!`;
        $('upload-result-detail').textContent =
          `Extracted ${data.records_extracted} parameter records.`;
        $('upload-meta-row').innerHTML = `
          <div class="upload-meta-item">📅 Date: <span>${formatDate(data.detected_date)}</span></div>
          <div class="upload-meta-item">🕐 Shift: <span>${data.detected_shift}</span></div>
          <div class="upload-meta-item">⏳ Expires in 7 days</div>
        `;
        showToast('Report saved successfully!');
        state.selectedFile = null;
        fileInput.value = '';
        $('file-selected').classList.remove('show');
      } else {
        box.className = 'upload-result error';
        $('upload-result-title').textContent = '❌ Upload failed';
        $('upload-result-detail').textContent = data.error || 'Unknown error.';
        $('upload-meta-row').innerHTML = '';
        showToast(data.error || 'Upload failed.', 'error');
      }
    } catch (err) {
      showToast('Network error. Is the server running?', 'error');
    }

    $('upload-btn-text').innerHTML = '<i data-feather="database"></i> Parse to DB';
    feather.replace();
    $('upload-btn').disabled = false;
  });
}

/* ═══ Chat ════════════════════════════════════════════════════════════════════ */

function appendMessage(role, text) {
  const messages = $('chat-messages');
  const div = document.createElement('div');
  div.className = `message ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.innerHTML = role === 'user' ? '<i data-feather="user"></i>' : '<i data-feather="database"></i>';

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble markdown-body';
  bubble.innerHTML = (role === 'assistant' && typeof marked !== 'undefined') ? marked.parse(text) : escapeHtml(text);

  div.appendChild(avatar);
  div.appendChild(bubble);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  feather.replace();
  return div;
}

function showTyping() {
  const messages = $('chat-messages');
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-indicator';

  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.innerHTML = '<i data-feather="database"></i>';

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble typing-dots';
  bubble.innerHTML = '<span></span><span></span><span></span>';

  div.appendChild(avatar);
  div.appendChild(bubble);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  feather.replace();
}

function removeTyping() {
  const el = $('typing-indicator');
  if (el) el.remove();
}

async function sendMessage(question) {
  if (!question.trim() || state.sending) return;
  state.sending = true;
  $('chat-send-btn').disabled = true;
  $('chat-input').value = '';

  appendMessage('user', question);
  showTyping();

  try {
    const res  = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    removeTyping();

    const answer = data.response || data.error || 'No response received.';
    appendMessage('assistant', answer);

    // Save to history
    state.chatHistory.unshift({
      question,
      answer,
      ts: new Date().toLocaleString('en-GB', {
        day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'
      }),
    });
    if (state.chatHistory.length > 50) state.chatHistory.pop();
    localStorage.setItem('lab_history', JSON.stringify(state.chatHistory));

  } catch {
    removeTyping();
    appendMessage('assistant', '⚠️ Could not reach the server. Is it running?');
  }

  state.sending = false;
  $('chat-send-btn').disabled = false;
}

if ($('chat-send-btn')) {
  $('chat-send-btn').addEventListener('click', () => {
    sendMessage($('chat-input').value.trim());
  });

  $('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage($('chat-input').value.trim());
    }
  });

  // Auto-resize textarea
  $('chat-input').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
}

/* ═══ History ═════════════════════════════════════════════════════════════════ */

function renderHistory(filter = '') {
  const list = $('history-list');
  const items = filter
    ? state.chatHistory.filter(h =>
        h.question.toLowerCase().includes(filter.toLowerCase()))
    : state.chatHistory;

  if (!items.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon"><i data-feather="message-circle"></i></div><p>No queries yet.</p></div>`;
    feather.replace();
    return;
  }

  list.innerHTML = items.map((h, i) => `
    <div class="history-item" onclick="toggleHistory(${i})">
      <div class="history-question">
        <span style="opacity:0.5;"><i data-feather="message-circle"></i></span>
        <span class="history-q-text">${escapeHtml(h.question)}</span>
        <span class="history-ts">${h.ts}</span>
      </div>
      <div class="history-answer markdown-body" id="hist-ans-${i}">
        ${typeof marked !== 'undefined' ? marked.parse(h.answer) : escapeHtml(h.answer)}
      </div>
    </div>
  `).join('');
  feather.replace();
}

window.toggleHistory = function(i) {
  const el = $(`hist-ans-${i}`);
  if (el) el.classList.toggle('open');
};

if ($('history-search')) {
  $('history-search').addEventListener('input', function () {
    renderHistory(this.value);
  });
}

if ($('clear-history-btn')) {
  $('clear-history-btn').addEventListener('click', () => {
    if (!confirm('Clear all history?')) return;
    state.chatHistory = [];
    localStorage.removeItem('lab_history');
    renderHistory();
    showToast('History cleared.');
  });
}

/* ═══ API Status ══════════════════════════════════════════════════════════════ */
function updateApiStatus(ok) {
  $('api-dot').className = `status-dot ${ok ? 'ok' : 'err'}`;
  $('api-label').textContent = ok ? 'Connected' : 'Offline';
}

/* ═══ Init ════════════════════════════════════════════════════════════════════ */
checkStatus();
