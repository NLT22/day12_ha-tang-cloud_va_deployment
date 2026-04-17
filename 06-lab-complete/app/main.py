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
