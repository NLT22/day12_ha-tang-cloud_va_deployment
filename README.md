# Day 12 — Production AI Agent Deployment

> **Student:** Nguyễn Lê Trung  
> **Student ID:** 2A202600174   
> **Deployed:** https://2a202600174nguyenletrung-production.up.railway.app/ui

---

## Live Demo

| Endpoint | Mô tả |
|----------|-------|
| [`/ui`](https://2a202600174nguyenletrung-production.up.railway.app/ui) | Chat UI + test panel |
| [`/health`](https://2a202600174nguyenletrung-production.up.railway.app/health) | Liveness check |
| [`/docs`](https://2a202600174nguyenletrung-production.up.railway.app/docs) | API docs (dev only) |

---

## Project — `06-lab-complete/`

Production-ready AI agent kết hợp tất cả concepts từ Day 12.

```
06-lab-complete/
├── app/
│   ├── main.py          # FastAPI app, endpoints, session store
│   ├── config.py        # pydantic-settings, 12-factor config
│   ├── auth.py          # API Key authentication (401/403)
│   ├── rate_limiter.py  # Sliding window, 3 req/min
│   └── cost_guard.py    # Budget $10/month per user
├── utils/
│   ├── openai_llm.py    # OpenAI gpt-4o-mini wrapper
│   └── mock_llm.py      # Mock LLM (không cần API key)
├── Dockerfile           # Multi-stage build (~57 MB)
├── docker-compose.yml   # nginx + 3 agent replicas + redis
├── nginx.conf           # Round-robin load balancer
└── railway.toml         # Railway deployment config
```

### Features

| Feature | Implementation |
|---------|---------------|
| REST API + conversation history | `POST /ask` với `session_id` |
| API Key authentication | Header `X-API-Key` → 401/403 |
| Rate limiting | Sliding window 3 req/min, Redis-backed |
| Cost guard | $10/month per user, TTL Redis key |
| Health + Readiness | `GET /health`, `GET /ready` |
| Graceful shutdown | SIGTERM handler + uvicorn 30s timeout |
| Stateless design | Session lưu trong Redis (fallback in-memory) |
| Structured logging | JSON format `{"ts":..,"lvl":..,"msg":..}` |
| Multi-stage Docker | builder → runtime, non-root user |
| Load balancing | Nginx round-robin, 3 replicas (local) |
| Chat UI | `GET /ui` — dark mode, quick test panel |

---

## Chạy Local

### Docker Compose (full stack)

```bash
cd 06-lab-complete
cp .env.local .env        # thêm OPENAI_API_KEY nếu muốn dùng thật
docker compose up --build --scale agent=3 -d

curl http://localhost/health
curl -X POST http://localhost/ask \
  -H "X-API-Key: secret-key-123" \
  -H "Content-Type: application/json" \
  -d '{"question": "Hello"}'
```

### Không Docker

```bash
cd 06-lab-complete
pip install -r requirements.txt
cp .env.local .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Architecture

```
Local Docker Compose:
  [Nginx :80] → round-robin → [agent1 | agent2 | agent3] → [Redis]

Railway (production):
  [Railway HTTPS] → [uvicorn 1 worker] → [OpenAI gpt-4o-mini]
                                        → [in-memory store]
```

---

## Tài Liệu

| File | Nội dung |
|------|----------|
| [MISSION_ANSWERS.md](MISSION_ANSWERS.md) | Trả lời tất cả exercises Part 1-6 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Deployment info, test commands, Railway steps |