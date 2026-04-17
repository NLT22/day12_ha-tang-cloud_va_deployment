"""
Production AI Agent — Part 6 Final Project

Checklist:
  ✅ Config từ environment variables (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication (app/auth.py)
  ✅ Rate limiting 10 req/min per user (app/rate_limiter.py)
  ✅ Cost guard $10/month per user (app/cost_guard.py)
  ✅ Health check endpoint (/health)
  ✅ Readiness check endpoint (/ready)
  ✅ Graceful shutdown (SIGTERM)
  ✅ Stateless design — conversation history trong Redis
  ✅ Support conversation history (session_id)
  ✅ Structured JSON logging
  ✅ Input validation (Pydantic)
  ✅ Security headers + CORS
"""
import json
import logging
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, get_monthly_spend
from app.rate_limiter import check_rate_limit

# Auto-switch: dùng OpenAI thật nếu có API key, fallback mock
if settings.openai_api_key:
    from utils.openai_llm import ask as _openai_ask
    def llm_ask(question: str, history: list | None = None) -> str:
        return _openai_ask(question, history=history,
                           model=settings.llm_model,
                           api_key=settings.openai_api_key)
else:
    from utils.mock_llm import ask as _mock_ask
    def llm_ask(question: str, history: list | None = None) -> str:
        return _mock_ask(question)

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0

# ─────────────────────────────────────────────────────────
# Redis session store (stateless design)
# ─────────────────────────────────────────────────────────
_redis = None
_use_redis = False
_memory_store: dict = {}

if settings.redis_url:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(settings.redis_url, decode_responses=True)
        _redis.ping()
        _use_redis = True
        logger.info(json.dumps({"event": "redis_connected", "url": settings.redis_url}))
    except Exception as e:
        logger.warning(json.dumps({"event": "redis_unavailable", "reason": str(e)}))


def _session_save(session_id: str, data: dict, ttl: int = 3600):
    if _use_redis:
        _redis.setex(f"session:{session_id}", ttl, json.dumps(data))
    else:
        _memory_store[f"session:{session_id}"] = data


def _session_load(session_id: str) -> dict:
    if _use_redis:
        raw = _redis.get(f"session:{session_id}")
        return json.loads(raw) if raw else {}
    return _memory_store.get(f"session:{session_id}", {})


def _history_append(session_id: str, role: str, content: str) -> list:
    session = _session_load(session_id)
    history = session.get("history", [])
    history.append({
        "role": role,
        "content": content,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    if len(history) > 20:
        history = history[-20:]
    session["history"] = history
    _session_save(session_id, session)
    return history


# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "storage": "redis" if _use_redis else "in-memory",
    }))
    time.sleep(0.1)
    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown"}))


# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if "server" in response.headers:
            del response.headers["server"]
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration,
        }))
        return response
    except Exception:
        _error_count += 1
        raise


# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = Field(None, description="Omit to start a new session")


class AskResponse(BaseModel):
    question: str
    answer: str
    model: str
    session_id: str
    turn: int
    storage: str
    timestamp: str


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask  (X-API-Key required)",
            "history": "GET /chat/{session_id}/history  (X-API-Key required)",
            "health": "GET /health",
            "ready": "GET /ready",
            "metrics": "GET /metrics  (X-API-Key required)",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    user_id: str = Depends(verify_api_key),
):
    """
    Gửi câu hỏi đến AI agent.

    - **X-API-Key** header bắt buộc
    - Gửi **session_id** để tiếp tục cuộc trò chuyện
    - Bỏ trống session_id để bắt đầu session mới
    """
    # Rate limit — 10 req/min per user (từ rate_limiter.py)
    check_rate_limit(user_id)

    # Cost guard — $10/month per user (từ cost_guard.py)
    estimated_cost = len(body.question.split()) * 0.000002
    check_budget(user_id, estimated_cost)

    # Session management — stateless, state trong Redis
    session_id = body.session_id or str(uuid.uuid4())
    _history_append(session_id, "user", body.question)

    logger.info(json.dumps({
        "event": "agent_call",
        "session_id": session_id,
        "q_len": len(body.question),
        "client": str(request.client.host) if request.client else "unknown",
    }))

    # Lấy history hiện tại để truyền context cho OpenAI
    current_session = _session_load(session_id)
    answer = llm_ask(body.question, history=current_session.get("history", []))

    # Truncate answer to max_answer_words
    words = answer.split()
    if len(words) > settings.max_answer_words:
        answer = " ".join(words[:settings.max_answer_words]) + "…"

    output_cost = len(answer.split()) * 0.000006
    check_budget(user_id, output_cost)

    history = _history_append(session_id, "assistant", answer)
    turn = len([m for m in history if m["role"] == "user"])

    return AskResponse(
        question=body.question,
        answer=answer,
        model=settings.llm_model,
        session_id=session_id,
        turn=turn,
        storage="redis" if _use_redis else "in-memory",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/chat/{session_id}/history", tags=["Agent"])
def get_history(session_id: str, user_id: str = Depends(verify_api_key)):
    """Xem conversation history của một session."""
    session = _session_load(session_id)
    if not session:
        raise HTTPException(404, f"Session {session_id} not found or expired")
    return {
        "session_id": session_id,
        "messages": session.get("history", []),
        "count": len(session.get("history", [])),
    }


@app.delete("/chat/{session_id}", tags=["Agent"])
def delete_session(session_id: str, user_id: str = Depends(verify_api_key)):
    """Xóa session (user logout)."""
    if _use_redis:
        _redis.delete(f"session:{session_id}")
    else:
        _memory_store.pop(f"session:{session_id}", None)
    return {"deleted": session_id}


@app.get("/health", tags=["Operations"])
def health():
    """Liveness probe — platform restart container nếu fail."""
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": {
            "llm": "mock" if not settings.openai_api_key else "openai",
            "storage": "redis" if _use_redis else "in-memory",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    """Readiness probe — load balancer dừng route nếu not ready."""
    if not _is_ready:
        raise HTTPException(503, "Not ready yet")
    if _use_redis:
        try:
            _redis.ping()
        except Exception:
            raise HTTPException(503, "Redis not available")
    return {"ready": True, "storage": "redis" if _use_redis else "in-memory"}


@app.get("/debug-rate", tags=["Operations"])
def debug_rate(user_id: str = Depends(verify_api_key)):
    """Debug: show raw rate limiter state for this user."""
    from app.rate_limiter import _windows, _use_redis, _redis
    import time
    now = time.time()
    if _use_redis:
        key = f"ratelimit:{user_id}"
        entries = _redis.zrangebyscore(key, now - 60, "+inf")
        return {"backend": "redis", "count_in_window": len(entries), "limit": settings.rate_limit_per_minute}
    dq = _windows.get(user_id, [])
    recent = [t for t in dq if t >= now - 60]
    return {
        "backend": "in-memory",
        "count_in_window": len(recent),
        "limit": settings.rate_limit_per_minute,
        "worker_pid": __import__("os").getpid(),
    }


@app.get("/metrics", tags=["Operations"])
def metrics(user_id: str = Depends(verify_api_key)):
    """Basic metrics — protected."""
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "monthly_spend_usd": round(get_monthly_spend(user_id), 6),
        "monthly_budget_usd": settings.monthly_budget_usd,
    }


# ─────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────
_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Production AI Agent — Demo UI</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  header { background: #1e293b; border-bottom: 1px solid #334155; padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1.1rem; font-weight: 600; }
  .badge { font-size: .7rem; padding: 2px 8px; border-radius: 99px; background: #22c55e22; color: #22c55e; border: 1px solid #22c55e44; }
  .layout { display: grid; grid-template-columns: 1fr 340px; gap: 0; height: calc(100vh - 57px); }
  /* Chat panel */
  .chat-panel { display: flex; flex-direction: column; border-right: 1px solid #334155; }
  .chat-messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 12px; font-size: .9rem; line-height: 1.5; word-break: break-word; }
  .msg.user { background: #3b82f6; align-self: flex-end; border-bottom-right-radius: 3px; }
  .msg.bot  { background: #1e293b; border: 1px solid #334155; align-self: flex-start; border-bottom-left-radius: 3px; }
  .msg.error{ background: #7f1d1d; border: 1px solid #ef444444; align-self: flex-start; }
  .msg .meta { font-size: .7rem; opacity: .6; margin-top: 4px; }
  .chat-input-bar { display: flex; gap: 8px; padding: 14px 16px; border-top: 1px solid #334155; background: #1e293b; }
  .chat-input-bar input { flex: 1; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: .9rem; outline: none; }
  .chat-input-bar input:focus { border-color: #3b82f6; }
  .chat-input-bar button { background: #3b82f6; border: none; border-radius: 8px; padding: 10px 18px; color: #fff; font-weight: 600; cursor: pointer; font-size: .9rem; }
  .chat-input-bar button:disabled { opacity: .5; cursor: not-allowed; }
  /* Test panel */
  .test-panel { overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; background: #0f172a; }
  .test-panel h2 { font-size: .85rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: #94a3b8; margin-bottom: 4px; }
  .api-key-row { display: flex; gap: 6px; }
  .api-key-row input { flex: 1; background: #1e293b; border: 1px solid #334155; border-radius: 6px; padding: 7px 10px; color: #e2e8f0; font-size: .82rem; outline: none; }
  .test-btn { width: 100%; background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 9px 12px; color: #e2e8f0; cursor: pointer; text-align: left; font-size: .82rem; display: flex; justify-content: space-between; align-items: center; }
  .test-btn:hover { border-color: #3b82f6; color: #93c5fd; }
  .result-box { background: #0a0f1e; border: 1px solid #1e293b; border-radius: 6px; padding: 10px; font-size: .75rem; font-family: monospace; white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow-y: auto; color: #94a3b8; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
  .dot-green { background: #22c55e; } .dot-red { background: #ef4444; } .dot-yellow { background: #eab308; }
  .stat-row { display: flex; justify-content: space-between; font-size: .8rem; padding: 4px 0; border-bottom: 1px solid #1e293b; }
  .stat-row:last-child { border: none; }
  .stat-val { color: #38bdf8; font-weight: 600; }
  .section { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px; }
  .new-session-btn { font-size: .75rem; padding: 4px 10px; background: #334155; border: none; border-radius: 6px; color: #94a3b8; cursor: pointer; }
  .new-session-btn:hover { color: #e2e8f0; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #334155; border-top-color: #3b82f6; border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <span style="font-size:1.3rem">🤖</span>
  <h1>Production AI Agent</h1>
  <span class="badge" id="env-badge">loading…</span>
  <span style="flex:1"></span>
  <span style="font-size:.8rem;color:#64748b">Rate limit: 3 req/min &nbsp;|&nbsp; Max 80 words</span>
</header>
<div class="layout">
  <!-- Chat -->
  <div class="chat-panel">
    <div class="chat-messages" id="messages">
      <div class="msg bot">👋 Xin chào! Nhập API key bên phải rồi bắt đầu chat.<div class="meta">System</div></div>
    </div>
    <div class="chat-input-bar">
      <input id="q-input" type="text" placeholder="Nhập câu hỏi… (Enter để gửi)" />
      <button id="send-btn" onclick="sendMessage()">Gửi ➤</button>
    </div>
  </div>
  <!-- Test panel -->
  <div class="test-panel">
    <!-- API Key -->
    <div class="section">
      <h2>🔑 API Key</h2>
      <div class="api-key-row" style="margin-top:8px">
        <input id="api-key" type="password" placeholder="X-API-Key" value="secret-key-123" />
        <button class="new-session-btn" onclick="newSession()">New session</button>
      </div>
      <div style="font-size:.72rem;color:#475569;margin-top:5px" id="session-id-display">session: (new)</div>
    </div>

    <!-- Quick tests -->
    <div class="section">
      <h2>🧪 Quick Tests</h2>
      <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
        <button class="test-btn" onclick="runTest('health')">GET /health <span>▶</span></button>
        <button class="test-btn" onclick="runTest('ready')">GET /ready <span>▶</span></button>
        <button class="test-btn" onclick="runTest('no-auth')">POST /ask (no key) → 401 <span>▶</span></button>
        <button class="test-btn" onclick="runTest('wrong-key')">POST /ask (wrong key) → 403 <span>▶</span></button>
        <button class="test-btn" onclick="runTest('rate-limit')">Rate limit (5 rapid) → 429 <span>▶</span></button>
        <button class="test-btn" onclick="runTest('metrics')">GET /metrics <span>▶</span></button>
      </div>
    </div>

    <!-- Test result -->
    <div class="section">
      <h2>📋 Test Result</h2>
      <div class="result-box" id="result-box" style="margin-top:8px">Run a test above…</div>
    </div>

    <!-- Live stats -->
    <div class="section">
      <h2>📊 Live Stats</h2>
      <div id="stats-box" style="margin-top:6px">
        <div class="stat-row"><span>Status</span><span class="stat-val" id="s-status">—</span></div>
        <div class="stat-row"><span>Uptime</span><span class="stat-val" id="s-uptime">—</span></div>
        <div class="stat-row"><span>Total requests</span><span class="stat-val" id="s-reqs">—</span></div>
        <div class="stat-row"><span>LLM</span><span class="stat-val" id="s-llm">—</span></div>
        <div class="stat-row"><span>Storage</span><span class="stat-val" id="s-storage">—</span></div>
        <div class="stat-row"><span>Monthly spend</span><span class="stat-val" id="s-spend">—</span></div>
      </div>
      <button class="test-btn" style="margin-top:8px" onclick="refreshStats()">↻ Refresh</button>
    </div>
  </div>
</div>

<script>
  let sessionId = null;
  const BASE = '';

  function newSession() {
    sessionId = null;
    document.getElementById('session-id-display').textContent = 'session: (new)';
    appendMsg('bot', '🆕 Session reset. Next message starts a new conversation.');
  }

  function appendMsg(type, text, meta='') {
    const box = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = 'msg ' + type;
    div.innerHTML = text.replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>') +
      (meta ? `<div class="meta">${meta}</div>` : '');
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function setLoading(on) {
    const btn = document.getElementById('send-btn');
    btn.disabled = on;
    btn.innerHTML = on ? '<span class="spinner"></span>' : 'Gửi ➤';
  }

  async function sendMessage() {
    const input = document.getElementById('q-input');
    const apiKey = document.getElementById('api-key').value.trim();
    const q = input.value.trim();
    if (!q) return;
    if (!apiKey) { appendMsg('error', '⚠ Nhập API key trước!'); return; }

    input.value = '';
    appendMsg('user', q);
    setLoading(true);

    const body = { question: q };
    if (sessionId) body.session_id = sessionId;

    try {
      const r = await fetch(BASE + '/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify(body)
      });
      const data = await r.json();
      if (!r.ok) {
        appendMsg('error', `❌ ${r.status}: ${data.detail || JSON.stringify(data)}`);
      } else {
        sessionId = data.session_id;
        document.getElementById('session-id-display').textContent = 'session: ' + sessionId.slice(0,8) + '…  turn ' + data.turn;
        appendMsg('bot', data.answer, `${data.model} · ${data.storage} · turn ${data.turn}`);
      }
    } catch(e) {
      appendMsg('error', '❌ Network error: ' + e.message);
    }
    setLoading(false);
  }

  document.getElementById('q-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  async function runTest(name) {
    const apiKey = document.getElementById('api-key').value.trim();
    const box = document.getElementById('result-box');
    box.textContent = 'Running…';

    let method = 'GET', url = '', headers = {}, body = null, label = name;

    if (name === 'health') { url = '/health'; label = 'GET /health'; }
    else if (name === 'ready') { url = '/ready'; label = 'GET /ready'; }
    else if (name === 'metrics') {
      url = '/metrics'; headers['X-API-Key'] = apiKey; label = 'GET /metrics';
    }
    else if (name === 'no-auth') {
      method = 'POST'; url = '/ask';
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify({question: 'test'});
      label = 'POST /ask (no key) → expect 401';
    }
    else if (name === 'wrong-key') {
      method = 'POST'; url = '/ask';
      headers = {'Content-Type':'application/json','X-API-Key':'totally-wrong'};
      body = JSON.stringify({question:'test'});
      label = 'POST /ask (wrong key) → expect 403';
    }
    else if (name === 'rate-limit') {
      box.textContent = 'Firing 5 rapid requests…\\n';
      const results = [];
      for (let i = 0; i < 5; i++) {
        const r = await fetch('/ask', {
          method: 'POST',
          headers: {'Content-Type':'application/json','X-API-Key': apiKey},
          body: JSON.stringify({question:'rate limit test'})
        });
        results.push(`req ${i+1}: HTTP ${r.status}`);
      }
      box.textContent = results.join('\\n') + '\\n\\n(expect 429 after 3 within 60s)';
      return;
    }

    try {
      const r = await fetch(BASE + url, { method, headers, body });
      const text = await r.text();
      let pretty;
      try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { pretty = text; }
      box.textContent = `${label}\\nHTTP ${r.status}\\n\\n${pretty}`;
    } catch(e) {
      box.textContent = 'Error: ' + e.message;
    }
  }

  async function refreshStats() {
    try {
      const r = await fetch('/health');
      const d = await r.json();
      document.getElementById('s-status').textContent = d.status === 'ok' ? '✅ ok' : '❌ ' + d.status;
      document.getElementById('s-uptime').textContent = d.uptime_seconds + 's';
      document.getElementById('s-reqs').textContent = d.total_requests;
      document.getElementById('s-llm').textContent = d.checks?.llm || '—';
      document.getElementById('s-storage').textContent = d.checks?.storage || '—';
      document.getElementById('env-badge').textContent = d.environment;

      const apiKey = document.getElementById('api-key').value.trim();
      if (apiKey) {
        const m = await fetch('/metrics', {headers:{'X-API-Key':apiKey}});
        if (m.ok) {
          const md = await m.json();
          document.getElementById('s-spend').textContent = '$' + md.monthly_spend_usd;
        }
      }
    } catch(e) { document.getElementById('s-status').textContent = '❌ offline'; }
  }

  // Auto-refresh stats every 10s
  refreshStats();
  setInterval(refreshStats, 10000);
</script>
</body>
</html>"""


@app.get("/ui", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
def ui():
    """Chat UI + test panel."""
    return HTMLResponse(content=_UI_HTML)


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_sigterm(signum, _frame):
    logger.info(json.dumps({"event": "signal_received", "signum": signum}))
    # uvicorn bắt SIGTERM và chạy lifespan shutdown (chờ in-flight requests)


signal.signal(signal.SIGTERM, _handle_sigterm)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
