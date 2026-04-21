from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
import requests
import time
import json
import os

app = Flask(__name__)

# Sử dụng API key từ environment variable (bảo mật hơn)
API_KEY = os.getenv("API_KEY", "xai-Gi2LhpN8cudlMMVsVa4lLksh0xOcjd3X74vCy2U8n5SwnRwB7MYQ1U4uf9a9fu8fwNQ5m3Ui2mWOTPJs")
API_URL = "https://api.x.ai/v1/chat/completions"
MODEL = "grok-4"

# ====================== SYSTEM PROMPTS ======================
def build_system_prompt(lang, subject, mode):
    is_vi = lang == "vi"
    lang_str = "tieng Viet, vui tuoi, de hieu, co vi du cu the" if is_vi else "English, friendly, clear, with concrete examples"

    base = f"Ban la gia su {subject} xuat sac danh cho hoc sinh cap 2 Viet Nam. Luon tra loi bang {lang_str}."

    modes = {
        "explain": (
            f"{base} NHIEM VU: Giai thich khai niem tung buoc ro rang. Su dung: (1) Dinh nghia don gian, (2) Vi du doi thuong, (3) Tom tat cuoi cung. Viet co dau day du."
            if is_vi else
            f"{base} TASK: Explain concepts step-by-step. Use: (1) Simple definition, (2) Real-life example, (3) Summary. Always structured."
        ),
        "hint": (
            f"{base} NHIEM VU: Chi goi y, KHONG giai thang. Dat cau hoi dan dat de hoc sinh tu suy nghi. Toi da 3 goi y ngan."
            if is_vi else
            f"{base} TASK: Give hints ONLY, never solve directly. Ask guiding questions. Max 3 short hints."
        ),
        "check": (
            f"{base} NHIEM VU: Cham bai hoc sinh. Neu dung: khen ngo ro rang. Neu sai: chi ra dung cho sai, huong dan sua, giai dung lai. Cu the, chi tiet."
            if is_vi else
            f"{base} TASK: Grade student's work. If correct: praise clearly. If wrong: point out the error, guide correction, show correct solution."
        ),
        "practice": (
            f"{base} NHIEM VU: Tao 3 bai tap phu hop cap do hoc sinh. Moi bai: (1) De bai ro rang, (2) Do kho tang dan, (3) Co goi y nho. KHONG giai ngay."
            if is_vi else
            f"{base} TASK: Create 3 practice problems with increasing difficulty. Each: clear statement + small hint. Do NOT solve them."
        ),
        "quick": (
            f"{base} NHIEM VU: Tra loi NGAN GON, toi da 3 dong. Thang tay, khong giai thich dai. Chi dap an + cong thuc neu can."
            if is_vi else
            f"{base} TASK: Answer BRIEFLY, max 3 lines. Direct answer + formula if needed. No lengthy explanation."
        ),
    }
    return modes.get(mode, modes["explain"])

# ====================== TOKEN / TEMP CONFIG PER MODE ======================
MODE_CONFIG = {
    "explain":  {"max_tokens": 2000, "temperature": 0.75},
    "hint":     {"max_tokens": 700,  "temperature": 0.6},
    "check":    {"max_tokens": 1500, "temperature": 0.5},
    "practice": {"max_tokens": 1200, "temperature": 0.8},
    "quick":    {"max_tokens": 400,  "temperature": 0.3},
}

# ====================== HTML ======================
HTML = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StudyMate AI Pro - Gia Sư AI Thông Minh</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🤖</text></svg>">
<style>
:root {
  --primary: #22c55e;
  --primary-dim: #16a34a;
  --dark: #0f172a;
  --card: #1e293b;
  --card2: #263348;
  --border: #334155;
  --text: #e2e8f0;
  --sub: #94a3b8;
  --user-bg: #374151;
  --bot-bg: #166534;
  --bot-text: #dcfce7;
  --loading-bg: #854d0e;
  --quick-bg: #0e7490;
}
[data-theme="light"] {
  --dark: #f1f5f9;
  --card: #ffffff;
  --card2: #f8fafc;
  --border: #cbd5e1;
  --text: #1e293b;
  --sub: #64748b;
  --user-bg: #475569;
  --bot-bg: #dcfce7;
  --bot-text: #14532d;
  --loading-bg: #fef9c3;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
  font-family: 'Segoe UI', system-ui, sans-serif; 
  background: var(--dark); 
  color: var(--text); 
  min-height: 100vh; 
  transition: background 0.3s, color 0.3s; 
}

/* ---- Layout ---- */
.app { display: flex; height: 100vh; overflow: hidden; }
.sidebar { 
  width: 240px; 
  background: var(--card); 
  border-right: 1px solid var(--border); 
  display: flex; 
  flex-direction: column; 
  padding: 14px; 
  gap: 8px; 
  flex-shrink: 0; 
}
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

/* ---- Header banner ---- */
.header-banner {
  background: linear-gradient(135deg, #22c55e, #16a34a);
  color: white;
  padding: 8px 16px;
  text-align: center;
  font-size: 0.9rem;
  font-weight: 600;
}

/* ---- Sidebar ---- */
.logo { 
  font-size: 1.1rem; 
  font-weight: 700; 
  background: linear-gradient(90deg, #22c55e, #a3e635); 
  -webkit-background-clip: text; 
  -webkit-text-fill-color: transparent; 
  padding: 4px 0 12px; 
  text-align: center; 
}
.sidebar-btn { 
  width: 100%; 
  padding: 10px 14px; 
  border: none; 
  border-radius: 10px; 
  background: var(--primary); 
  color: #052e16; 
  font-weight: 600; 
  cursor: pointer; 
  font-size: 0.95rem; 
  margin-bottom: 4px; 
  transition: background 0.2s; 
}
.sidebar-btn:hover { background: var(--primary-dim); color: white; }
.sidebar-label { 
  font-size: 0.75rem; 
  color: var(--sub); 
  text-transform: uppercase; 
  letter-spacing: 0.08em; 
  padding: 8px 4px 4px; 
}
.history-list { 
  flex: 1; 
  overflow-y: auto; 
  display: flex; 
  flex-direction: column; 
  gap: 4px; 
}
.history-item { 
  padding: 8px 10px; 
  border-radius: 8px; 
  cursor: pointer; 
  font-size: 0.85rem; 
  color: var(--text); 
  background: transparent; 
  border: 1px solid transparent; 
  white-space: nowrap; 
  overflow: hidden; 
  text-overflow: ellipsis; 
  transition: all 0.15s; 
}
.history-item:hover { background: var(--card2); border-color: var(--border); }
.history-item.active { background: var(--card2); border-color: var(--primary); color: var(--primary); }
.sidebar-actions { 
  display: flex; 
  gap: 6px; 
  padding-top: 8px; 
  border-top: 1px solid var(--border); 
}
.icon-btn { 
  flex: 1; 
  padding: 8px; 
  border: none; 
  border-radius: 8px; 
  background: var(--card2); 
  color: var(--sub); 
  cursor: pointer; 
  font-size: 1rem; 
  transition: all 0.2s; 
}
.icon-btn:hover { color: var(--text); background: var(--border); }

/* ---- Top bar ---- */
.topbar { 
  display: flex; 
  align-items: center; 
  gap: 8px; 
  padding: 12px 16px; 
  background: var(--card); 
  border-bottom: 1px solid var(--border); 
  flex-wrap: wrap; 
}
.topbar select { 
  padding: 8px 12px; 
  border: 1px solid var(--border); 
  border-radius: 8px; 
  background: var(--card2); 
  color: var(--text); 
  cursor: pointer; 
  font-size: 0.9rem; 
}
.topbar select:focus { outline: 2px solid var(--primary); }
.mode-quick { border-color: #22d3ee !important; color: #22d3ee !important; }
.badge-quick { 
  font-size: 0.7rem; 
  background: #0e7490; 
  color: white; 
  padding: 2px 6px; 
  border-radius: 99px; 
  margin-left: 4px; 
  vertical-align: middle; 
}

/* ---- Chat area ---- */
.chat-wrap { 
  flex: 1; 
  overflow-y: auto; 
  padding: 20px 16px; 
  display: flex; 
  flex-direction: column; 
  gap: 14px; 
}
.chat-wrap::-webkit-scrollbar { width: 5px; }
.chat-wrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }
.msg-row { display: flex; gap: 10px; max-width: 820px; width: 100%; }
.msg-row.user { align-self: flex-end; flex-direction: row-reverse; }
.msg-row.bot, .msg-row.loading { align-self: flex-start; }
.avatar { 
  width: 34px; 
  height: 34px; 
  border-radius: 50%; 
  display: flex; 
  align-items: center; 
  justify-content: center; 
  font-size: 0.9rem; 
  flex-shrink: 0; 
  font-weight: 700; 
}
.avatar.user-av { background: #475569; color: white; }
.avatar.bot-av  { background: var(--primary); color: #052e16; }
.bubble { 
  padding: 12px 16px; 
  border-radius: 14px; 
  line-height: 1.65; 
  font-size: 0.97rem; 
  max-width: 680px; 
}
.user .bubble  { background: var(--user-bg); color: white; border-bottom-right-radius: 4px; }
.bot .bubble   { background: var(--bot-bg); color: var(--bot-text); border-bottom-left-radius: 4px; }
.loading .bubble { background: var(--loading-bg); color: #92400e; border-bottom-left-radius: 4px; }
[data-theme="light"] .loading .bubble { color: #78350f; }
.bubble-meta { 
  display: flex; 
  align-items: center; 
  gap: 10px; 
  margin-top: 8px; 
  opacity: 0.8; 
}
.bubble-time { font-size: 0.72rem; }
.action-btns { display: flex; gap: 4px; }
.action-btns button { 
  background: none; 
  border: none; 
  cursor: pointer; 
  font-size: 0.8rem; 
  padding: 2px 5px; 
  border-radius: 4px; 
  color: inherit; 
  opacity: 0.8; 
  transition: opacity 0.2s; 
}
.action-btns button:hover { opacity: 1; }

/* Markdown styles inside bot bubble */
.bubble h1,.bubble h2,.bubble h3 { margin: 10px 0 4px; font-size: 1rem; }
.bubble ul,.bubble ol { margin: 6px 0 6px 18px; }
.bubble code { 
  background: rgba(0,0,0,0.15); 
  padding: 1px 5px; 
  border-radius: 4px; 
  font-family: monospace; 
  font-size: 0.9em; 
}
.bubble pre { 
  background: rgba(0,0,0,0.2); 
  padding: 10px; 
  border-radius: 8px; 
  overflow-x: auto; 
  margin: 8px 0; 
}
.bubble pre code { background: none; }
.bubble strong { font-weight: 700; }
.bubble p { margin: 4px 0; }
.bubble hr { border: none; border-top: 1px solid rgba(255,255,255,0.15); margin: 8px 0; }

/* Quick badge on bubble */
.quick-tag { 
  display: inline-block; 
  font-size: 0.7rem; 
  background: #0e7490; 
  color: white; 
  padding: 1px 7px; 
  border-radius: 99px; 
  margin-bottom: 4px; 
}

/* Streaming cursor */
.cursor { 
  display: inline-block; 
  width: 2px; 
  height: 1em; 
  background: currentColor; 
  margin-left: 2px; 
  animation: blink 0.8s step-end infinite; 
  vertical-align: text-bottom; 
}
@keyframes blink { 50% { opacity: 0; } }

/* Dot loading */
.dot { animation: bounce 1.2s infinite; display: inline-block; }
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,80%,100%{transform:translateY(0)} 40%{transform:translateY(-6px)} }

/* ---- Input area ---- */
.input-wrap { 
  padding: 12px 16px; 
  background: var(--card); 
  border-top: 1px solid var(--border); 
  display: flex; 
  gap: 10px; 
  align-items: flex-end; 
}
#inputBox { 
  flex: 1; 
  padding: 13px 16px; 
  border: 1px solid var(--border); 
  border-radius: 12px; 
  background: var(--card2); 
  color: var(--text); 
  font-size: 1rem; 
  resize: none; 
  max-height: 140px; 
  min-height: 50px; 
  overflow-y: auto; 
  font-family: inherit; 
  transition: border-color 0.2s; 
}
#inputBox:focus { outline: none; border-color: var(--primary); }
.round-btn { 
  width: 48px; 
  height: 48px; 
  border-radius: 50%; 
  border: none; 
  cursor: pointer; 
  display: flex; 
  align-items: center; 
  justify-content: center; 
  font-size: 1.2rem; 
  flex-shrink: 0; 
  transition: all 0.2s; 
}
.voice-btn { background: #ef4444; color: white; }
.voice-btn.recording { background: var(--primary); animation: pulse 1.5s infinite; }
.send-btn { background: var(--primary); color: #052e16; }
.send-btn:hover { background: var(--primary-dim); color: white; }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
@keyframes pulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.12)} }

/* ---- Responsive ---- */
@media (max-width: 640px) {
  .sidebar { display: none; }
  .topbar { gap: 5px; }
}
</style>
</head>
<body data-theme="dark">

<!-- HEADER BANNER -->
<div class="header-banner">
  🌟 StudyMate AI Pro - Gia Sư AI Thông Minh Nhất Việt Nam | 🔥 Hoàn toàn MIỄN PHÍ
</div>

<div class="app">

<!-- SIDEBAR -->
<div class="sidebar">
  <div class="logo"><i class="fas fa-robot"></i> StudyMate AI</div>
  <button class="sidebar-btn" onclick="newChat()"><i class="fas fa-plus"></i> Cuộc trò chuyện mới</button>
  <div class="sidebar-label">Lịch sử</div>
  <div class="history-list" id="historyList"></div>
  <div class="sidebar-actions">
    <button class="icon-btn" onclick="toggleTheme()" title="Đổi giao diện"><i class="fas fa-moon"></i></button>
    <button class="icon-btn" onclick="clearHistory()" title="Xóa lịch sử"><i class="fas fa-trash"></i></button>
  </div>
</div>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <select id="lang" onchange="onLangChange()">
      <option value="vi">🇻🇳 Tiếng Việt</option>
      <option value="en">🇬🇧 English</option>
    </select>
    <select id="subject">
      <option value="toan">📐 Toán Học</option>
      <option value="vat ly">⚡ Vật Lý</option>
      <option value="hoa hoc">🧪 Hóa Học</option>
      <option value="tieng anh">🇬🇧 Tiếng Anh</option>
      <option value="sinh hoc">🌱 Sinh Học</option>
      <option value="lich su">📚 Lịch Sử</option>
      <option value="dia ly">🌍 Địa Lý</option>
    </select>
    <select id="mode" onchange="onModeChange()">
      <option value="explain">📚 Giải thích chi tiết</option>
      <option value="hint">💡 Gợi ý</option>
      <option value="check">✅ Chấm bài</option>
      <option value="practice">📝 Tạo bài tập</option>
      <option value="quick">⚡ Trả lời nhanh</option>
    </select>
    <span id="quickBadge" class="badge-quick" style="display:none;">NHANH</span>
  </div>

  <div class="chat-wrap" id="chatWrap">
    <!-- Welcome Message -->
    <div class="msg-row bot">
      <div class="avatar bot-av">AI</div>
      <div class="bubble">
        <strong>🎉 Chào mừng bạn đến với StudyMate AI!</strong><br>
        Tôi là gia sư AI thông minh, sẵn sàng giúp bạn học tập hiệu quả:<br><br>
        📚 <strong>Giải thích chi tiết</strong> - Giải thích từng bước<br>
        💡 <strong>Gợi ý</strong> - Hướng dẫn tư duy<br>
        ✅ <strong>Chấm bài</strong> - Kiểm tra và sửa lỗi<br>
        📝 <strong>Tạo bài tập</strong> - Luyện tập thêm<br>
        ⚡ <strong>Trả lời nhanh</strong> - Giải đáp tức thì<br><br>
        Hãy chọn môn học và bắt đầu hỏi bất cứ điều gì bạn cần học! 🚀
      </div>
    </div>
  </div>

  <div class="input-wrap">
    <textarea id="inputBox" rows="1" placeholder="Hỏi bất cứ điều gì về bài học..." oninput="autoResize(this)"></textarea>
    <button class="round-btn voice-btn" id="voiceBtn" title="Nói"><i class="fas fa-microphone"></i></button>
    <button class="round-btn send-btn" id="sendBtn" onclick="sendMessage()" title="Gửi"><i class="fas fa-paper-plane"></i></button>
  </div>
</div>

</div>

<script>
marked.setOptions({ breaks: true, gfm: true });

// ===== STATE =====
let isLoading = false;
let recognition = null;
let currentChatId = null;
let chats = JSON.parse(localStorage.getItem('sm_chats') || '{}');
let conversationHistory = [];

// ===== VOICE =====
if ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.interimResults = false;
  recognition.onresult = e => {
    document.getElementById('inputBox').value = e.results[0][0].transcript;
    document.getElementById('voiceBtn').classList.remove('recording');
    sendMessage();
  };
  recognition.onerror = () => document.getElementById('voiceBtn').classList.remove('recording');
}
document.getElementById('voiceBtn').addEventListener('click', () => {
  if (!recognition) return alert("Trình duyệt không hỗ trợ giọng nói!");
  const btn = document.getElementById('voiceBtn');
  if (btn.classList.contains('recording')) { recognition.stop(); btn.classList.remove('recording'); }
  else {
    recognition.lang = document.getElementById('lang').value === 'vi' ? 'vi-VN' : 'en-US';
    recognition.start(); btn.classList.add('recording');
  }
});

// ===== THEME =====
function toggleTheme() {
  const t = document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.body.setAttribute('data-theme', t);
  localStorage.setItem('sm_theme', t);
}
if (localStorage.getItem('sm_theme') === 'light') document.body.setAttribute('data-theme', 'light');

// ===== MODE UI =====
function onModeChange() {
  const m = document.getElementById('mode').value;
  const badge = document.getElementById('quickBadge');
  badge.style.display = m === 'quick' ? 'inline-block' : 'none';
  document.getElementById('mode').className = m === 'quick' ? 'mode-quick' : '';
}
function onLangChange() {
  const p = document.getElementById('inputBox');
  p.placeholder = document.getElementById('lang').value === 'vi' ? 'Hỏi bất cứ điều gì về bài học...' : 'Ask anything about your lesson...';
}

// ===== ADD MESSAGE =====
function addMessage(text, type, isQuick = false, save = true) {
  const wrap = document.getElementById('chatWrap');
  const row = document.createElement('div');
  row.className = `msg-row ${type}`;

  const time = new Date().toLocaleTimeString('vi-VN', {hour:'2-digit', minute:'2-digit'});
  const avLabel = type === 'user' ? 'B' : 'AI';
  const avClass = type === 'user' ? 'user-av' : 'bot-av';

  const renderedText = type === 'bot' ? marked.parse(text) : escHtml(text);
  const quickTag = (type === 'bot' && isQuick) ? '<div class="quick-tag">NHANH</div>' : '';

  row.innerHTML = `
    <div class="avatar ${avClass}">${avLabel}</div>
    <div class="bubble">
      ${quickTag}
      <div class="content">${renderedText}</div>
      <div class="bubble-meta">
        <span class="bubble-time">${time}</span>
        ${type === 'bot' ? `<div class="action-btns">
          <button onclick="copyBubble(this)" title="Sao chép"><i class="fas fa-copy"></i></button>
          <button onclick="likeBubble(this)" title="Thích"><i class="fas fa-thumbs-up"></i></button>
          <button onclick="dislikeBubble(this)" title="Không thích"><i class="fas fa-thumbs-down"></i></button>
        </div>` : ''}
      </div>
    </div>`;

  wrap.appendChild(row);
  wrap.scrollTop = wrap.scrollHeight;

  if (save && currentChatId) {
    chats[currentChatId].messages.push({type, content: text, time, isQuick});
    if (chats[currentChatId].messages.length === 2) {
      chats[currentChatId].title = text.slice(0, 36) + (text.length > 36 ? '...' : '');
    }
    saveChats();
    renderHistory();
  }
}

// ===== STREAMING =====
function createStreamBubble(isQuick) {
  const wrap = document.getElementById('chatWrap');
  const row = document.createElement('div');
  row.className = 'msg-row bot';
  row.id = 'stream-row';
  const quickTag = isQuick ? '<div class="quick-tag">NHANH</div>' : '';
  row.innerHTML = `
    <div class="avatar bot-av">AI</div>
    <div class="bubble">
      ${quickTag}
      <div class="content" id="streamContent"></div>
      <span class="cursor" id="streamCursor"></span>
    </div>`;
  wrap.appendChild(row);
  wrap.scrollTop = wrap.scrollHeight;
}

// ===== LOADING =====
function showLoading() {
  const wrap = document.getElementById('chatWrap');
  const id = 'load-' + Date.now();
  const row = document.createElement('div');
  row.id = id;
  row.className = 'msg-row loading';
  row.innerHTML = `<div class="avatar bot-av">AI</div><div class="bubble">Đang suy nghĩ <span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></div>`;
  wrap.appendChild(row);
  wrap.scrollTop = wrap.scrollHeight;
  return id;
}

// ===== SEND MESSAGE =====
async function sendMessage() {
  const input = document.getElementById('inputBox');
  const message = input.value.trim();
  if (!message || isLoading) return;

  if (!currentChatId) newChat();

  const lang = document.getElementById('lang').value;
  const subject = document.getElementById('subject').value;
  const mode = document.getElementById('mode').value;
  const isQuick = mode === 'quick';

  conversationHistory.push({role: 'user', content: message});
  if (conversationHistory.length > 20) conversationHistory = conversationHistory.slice(-20);

  addMessage(message, 'user', false);
  input.value = ''; autoResize(input);
  isLoading = true;
  document.getElementById('sendBtn').disabled = true;

  if (isQuick) {
    const loadId = showLoading();
    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message, lang, subject, mode, history: conversationHistory.slice(0,-1)})
      });
      const data = await res.json();
      document.getElementById(loadId)?.remove();
      const reply = data.reply || 'Không có phản hồi.';
      conversationHistory.push({role: 'assistant', content: reply});
      addMessage(reply, 'bot', true);
    } catch(e) {
      document.getElementById(loadId)?.remove();
      addMessage('❌ Lỗi kết nối. Vui lòng thử lại!', 'bot', true);
    }
  } else {
    createStreamBubble(false);
    const contentEl = document.getElementById('streamContent');
    const cursorEl  = document.getElementById('streamCursor');
    let fullText = '';

    try {
      const res = await fetch('/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message, lang, subject, mode, history: conversationHistory.slice(0,-1)})
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        const lines = chunk.split('\n');
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const d = line.slice(6).trim();
            if (d === '[DONE]') continue;
            try {
              const obj = JSON.parse(d);
              const delta = obj.choices?.[0]?.delta?.content || '';
              fullText += delta;
              contentEl.innerHTML = marked.parse(fullText);
              document.getElementById('chatWrap').scrollTop = document.getElementById('chatWrap').scrollHeight;
            } catch {}
          }
        }
      }
    } catch(e) {
      fullText = '❌ Lỗi kết nối. Vui lòng thử lại!';
      contentEl.textContent = fullText;
    }

    cursorEl?.remove();
    const row = document.getElementById('stream-row');
    if (row) row.removeAttribute('id');

    conversationHistory.push({role: 'assistant', content: fullText});
    if (currentChatId) {
      const time = new Date().toLocaleTimeString('vi-VN', {hour:'2-digit', minute:'2-digit'});
      chats[currentChatId].messages.push({type:'bot', content: fullText, time, isQuick: false});
      if (chats[currentChatId].messages.length === 2)
        chats[currentChatId].title = message.slice(0,36) + (message.length>36?'...':'');
      saveChats(); renderHistory();
    }
  }

  isLoading = false;
  document.getElementById('sendBtn').disabled = false;
}

// ===== CHAT MANAGEMENT =====
function newChat() {
  currentChatId = 'c' + Date.now();
  conversationHistory = [];
  chats[currentChatId] = {title: 'Cuộc trò chuyện mới', messages: [], timestamp: new Date().toISOString()};
  const wrap = document.getElementById('chatWrap');
  wrap.innerHTML = `
    <div class="msg-row bot">
      <div class="avatar bot-av">AI</div>
      <div class="bubble">
        <strong>🎉 Chào mừng bạn đến với StudyMate AI!</strong><br>
        Tôi là gia sư AI thông minh, sẵn sàng giúp bạn học tập hiệu quả:<br><br>
        📚 <strong>Giải thích chi tiết</strong> - Giải thích từng bước<br>
        💡 <strong>Gợi ý</strong> - Hướng dẫn tư duy<br>
        ✅ <strong>Chấm bài</strong> - Kiểm tra và sửa lỗi<br>
        📝 <strong>Tạo bài tập</strong> - Luyện tập thêm<br>
        ⚡ <strong>Trả lời nhanh</strong> - Giải đáp tức thì<br><br>
        Hãy chọn môn học và bắt đầu hỏi bất cứ điều gì bạn cần học! 🚀
      </div>
    </div>`;
  saveChats(); renderHistory();
}

function saveChats() { localStorage.setItem('sm_chats', JSON.stringify(chats)); }

function renderHistory() {
  const el = document.getElementById('historyList');
  el.innerHTML = '';
  Object.keys(chats).reverse().forEach(id => {
    const div = document.createElement('div');
    div.className = 'history-item' + (id === currentChatId ? ' active' : '');
    div.textContent = chats[id].title || 'Không có tiêu đề';
    div.onclick = () => loadChat(id);
    el.appendChild(div);
  });
}

function loadChat(id) {
  currentChatId = id;
  conversationHistory = [];
  const wrap = document.getElementById('chatWrap');
  wrap.innerHTML = '';
  chats[id].messages.forEach(msg => {
    const row = document.createElement('div');
    row.className = `msg-row ${msg.type}`;
    const avLabel = msg.type === 'user' ? 'B' : 'AI';
    const avClass = msg.type === 'user' ? 'user-av' : 'bot-av';
    const rendered = msg.type === 'bot' ? marked.parse(msg.content) : escHtml(msg.content);
    const quickTag = (msg.type === 'bot' && msg.isQuick) ? '<div class="quick-tag">NHANH</div>' : '';
    row.innerHTML = `
      <div class="avatar ${avClass}">${avLabel}</div>
      <div class="bubble">
        ${quickTag}
        <div class="content">${rendered}</div>
        <div class="bubble-meta"><span class="bubble-time">${msg.time || ''}</span></div>
      </div>`;
    wrap.appendChild(row);
    conversationHistory.push({role: msg.type === 'user' ? 'user' : 'assistant', content: msg.content});
  });
  wrap.scrollTop = wrap.scrollHeight;
  renderHistory();
}

function clearHistory() {
  if (!confirm('Xóa toàn bộ lịch sử?')) return;
  chats = {};
  localStorage.removeItem('sm_chats');
  newChat();
}

// ===== ACTIONS =====
function copyBubble(btn) {
  const text = btn.closest('.bubble').querySelector('.content').innerText;
  navigator.clipboard.writeText(text);
  btn.innerHTML = '<i class="fas fa-check"></i>';
  setTimeout(() => btn.innerHTML = '<i class="fas fa-copy"></i>', 1500);
}
function likeBubble(btn) { btn.style.color = '#22c55e'; }
function dislikeBubble(btn) { btn.style.color = '#ef4444'; }

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

document.getElementById('inputBox').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ===== INIT =====
newChat();
onModeChange();
</script>
</body>
</html>
"""

# ====================== BACKEND ======================
@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    try:
        req = request.get_json()
        user_input = req.get("message", "").strip()
        if not user_input:
            return jsonify({"reply": "Bạn muốn hỏi gì nào?"})

        lang    = req.get("lang", "vi")
        subject = req.get("subject", "toan")
        mode    = req.get("mode", "quick")
        history = req.get("history", [])

        cfg = MODE_CONFIG.get(mode, MODE_CONFIG["quick"])
        system_prompt = build_system_prompt(lang, subject, mode)

        messages = [{"role": "system", "content": system_prompt}]
        for h in history[-10:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_input})

        payload = {
            "model": MODEL,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
            "messages": messages
        }

        for attempt in range(5):
            try:
                res = requests.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=25
                )
                if res.status_code == 429:
                    time.sleep(2 ** attempt); continue
                res.raise_for_status()
                return jsonify({"reply": res.json()["choices"][0]["message"]["content"]})
            except requests.exceptions.RequestException:
                if attempt == 4:
                    return jsonify({"reply": "❌ Server quá tải. Vui lòng thử lại sau."})
                time.sleep(2 ** attempt)

        return jsonify({"reply": "❌ Đã xảy ra lỗi."})
    except Exception as e:
        return jsonify({"reply": f"❌ Lỗi hệ thống: {str(e)}"})

@app.route("/stream", methods=["POST"])
def stream():
    req = request.get_json()
    user_input = req.get("message", "").strip()
    lang    = req.get("lang", "vi")
    subject = req.get("subject", "toan")
    mode    = req.get("mode", "explain")
    history = req.get("history", [])

    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["explain"])
    system_prompt = build_system_prompt(lang, subject, mode)

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_input})

    payload = {
        "model": MODEL,
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "stream": True,
        "messages": messages
    }

    def generate():
        try:
            with requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=60
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        decoded = line.decode("utf-8")
                        if decoded.startswith("data: "):
                            yield decoded + "\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'choices':[{'delta':{'content': f'❌ Lỗi: {str(e)}'}}]})}\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🌟 StudyMate AI Pro đã khởi động!")
    print(f"🔗 Local: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)