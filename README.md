# Sarvam Telecom Voice Bot
### AI-Powered Customer Support for Airtel — Built on Sarvam AI

> **Live Demo:** Coming Soon | **GitHub:** https://github.com/nrvm94/sarvam-telecom-bot

---

## What This Is

A production-ready voice support bot for Airtel customers. Customers speak Hindi or English, the bot understands, retrieves relevant knowledge from a curated Airtel knowledge base, and responds with natural-sounding voice — all in under 5 seconds. Complex issues auto-escalate to human agents via WhatsApp.

---

## Architecture Diagram

```
Browser (React) → FastAPI Backend → Sarvam STT → ChromaDB RAG
                                 ↓
                          Sarvam LLM → Sarvam TTS → Audio Response
                                 ↓
                    escalate? → n8n → Ticket + WhatsApp + Supabase
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system diagram.

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Frontend | React 18 + Vite + TailwindCSS | Voice capture UI |
| Backend | FastAPI + Python 3.11 | API orchestration |
| STT | Sarvam Saaras v3 (saaras:v3) | Hindi/English speech-to-text + auto language detection |
| LLM | Sarvam sarvam-105b | Response generation (reasoning model) |
| TTS | Sarvam Bulbul v3 (bulbul:v3) | Text-to-speech, 37+ Indian voices |
| Vector DB | ChromaDB | Semantic search over KB |
| Embeddings | sentence-transformers/all-MiniLM-L6-v2 | Document embeddings |
| Database | Supabase (PostgreSQL) | Call logs and conversation history |
| Automation | n8n (Docker) | Escalation workflow |
| Notifications | 360dialog WhatsApp API | Customer notifications |
| Mock Services | FastAPI (port 5000) | Downstream service simulation |

---

## Prerequisites

- Python 3.10+
- Node.js 18+
- npm 9+
- Docker Desktop (for n8n)
- Chrome browser (for microphone access via WebRTC)
- Git

---

## Local Setup

### 1. Clone the repository
```bash
git clone https://github.com/nrvm94/sarvam-telecom-bot.git
cd sarvam-telecom-bot
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and fill in your credentials
```

### 3. Backend Setup
```bash
cd backend
pip install -r requirements.txt
```

If any package fails with version conflicts, try:
```bash
pip install fastapi uvicorn python-dotenv pydantic aiohttp chromadb sentence-transformers supabase python-multipart
```

### 4. Start Backend
```bash
cd backend
python main.py
```
Verify: http://localhost:8000/health → `{"status":"ok"}`

### 5. Start Mock Server (separate terminal)
```bash
cd backend
python mock_server.py
```
Verify: `curl -X POST http://localhost:5000/mock/ticket`

### 6. Frontend Setup
```bash
cd frontend
npm install
```

### 7. Start Frontend
```bash
cd frontend
npm run dev
```
Verify: http://localhost:3000 → Airtel Support Bot UI

### 8. Start n8n (optional — for escalation workflow)
```bash
docker-compose up -d
```
Open http://localhost:5678 and follow [n8n/workflow_instructions.md](n8n/workflow_instructions.md)

---

## Running All Components

Open 3 terminal windows:

**Terminal 1 — Backend:**
```bash
cd backend && python main.py
```

**Terminal 2 — Mock Server:**
```bash
cd backend && python mock_server.py
```

**Terminal 3 — Frontend:**
```bash
cd frontend && npm run dev
```

**Terminal 4 (optional) — n8n:**
```bash
docker-compose up
```

---

## API Endpoints Quick Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check |
| POST | /voice/start | Start a call session |
| POST | /voice/transcribe | STT → RAG → LLM → TTS pipeline |
| POST | /voice/end | End call and save duration |
| POST | /n8n/webhook | n8n escalation callback |

Full API documentation: [docs/API_SPEC.md](docs/API_SPEC.md)

---

## Sarvam APIs Used

| API | Endpoint | Why Chosen |
|-----|----------|------------|
| Saaras v3 STT | `POST /speech-to-text` | Best Hindi/English code-mixing + auto language detection; 300ms latency; trained on Indian accents |
| Chat Completions (sarvam-105b) | `POST /v1/chat/completions` | Native Indian language reasoning model; TRAI-compliant in-country processing |
| Bulbul v3 TTS | `POST /text-to-speech` | 37+ Indian voices; natural Hindi/English synthesis; 8kHz telephony output |

---

## Environment Variables Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `SARVAM_API_KEY` | Sarvam AI API key | `sk_xxx...` |
| `SARVAM_API_BASE` | Sarvam API base URL | `https://api.sarvam.ai/v1` |
| `DIALOG_360_API_KEY` | 360dialog WhatsApp API key | `Z47M...` |
| `DIALOG_360_BASE_URL` | 360dialog base URL | `https://waba-sandbox.360dialog.io` |
| `SUPABASE_URL` | Supabase project URL | `https://xxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key | `eyJhbGci...` |
| `N8N_WEBHOOK_URL` | n8n escalation webhook | `http://localhost:5678/webhook/escalation` |
| `FRONTEND_URL` | Frontend URL for CORS | `http://localhost:3000` |
| `BACKEND_URL` | Backend URL | `http://localhost:8000` |
| `ENVIRONMENT` | Runtime environment | `development` |
| `LOG_LEVEL` | Python log level | `DEBUG` |
| `DEFAULT_LANGUAGE` | Default language | `hi` |
| `DEFAULT_TTS_VOICE` | Default TTS voice | `female_1` |

---

## Testing the Bot

1. Open http://localhost:3000 in Chrome
2. Select language (Hindi or English)
3. Click **Start Call**
4. Click the **🎤 microphone** button
5. Say something like:
   - Hindi: *"Mera balance kitna hai?"* (What is my balance?)
   - English: *"What is Airtel's 5G coverage?"*
   - Escalation test: *"I have a billing dispute and want a refund"*
6. Click mic again to stop — bot responds in 3–5 seconds
7. Click **End Call** when done

---

## n8n Escalation Workflow

Step-by-step setup guide: [n8n/workflow_instructions.md](n8n/workflow_instructions.md)

Quick test after setup:
```bash
curl -X POST http://localhost:5678/webhook/escalation \
  -H "Content-Type: application/json" \
  -d '{"call_id":"test_123","issue_type":"billing_dispute","user_query":"wrong charge","bot_response":"escalating"}'
```

---

## Business Case

Read the full business write-up: [docs/BUSINESS_WRITE_UP.md](docs/BUSINESS_WRITE_UP.md)

Key numbers:
- **Cost per AI call:** ₹0.40 vs ₹28 human call
- **Annual savings potential:** ₹745 crore (full deployment)
- **Response time:** 3–5 seconds vs 4–6 minute AHT

---

## Project Structure

```
sarvam-telecom-bot/
├── .env                          # Credentials (gitignored)
├── .env.example                  # Template for credentials
├── docker-compose.yml            # n8n service
├── README.md
├── backend/
│   ├── main.py                   # FastAPI app + pipeline orchestration
│   ├── sarvam_client.py          # STT + LLM + TTS API client
│   ├── rag_engine.py             # ChromaDB RAG engine
│   ├── conversation.py           # Escalation + issue classification
│   ├── supabase_client.py        # Database operations
│   ├── mock_server.py            # Mock downstream services
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── vite.config.js
│   ├── package.json
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       └── VoiceBot.jsx          # Main UI component
├── db/
│   └── airtel_kb.json            # 21-document Airtel knowledge base
├── n8n/
│   └── workflow_instructions.md  # n8n setup guide
└── docs/
    ├── Sarvam_AI_Airtel_VoiceBot_Neerav_Mahadane.pdf  # Slide deck (8 slides, CXO-ready)
    ├── BUSINESS_WRITE_UP.md      # Business case write-up for Airtel CTO
    ├── ARCHITECTURE.md           # System architecture
    └── API_SPEC.md               # API documentation
```

---

## Demo Video

**[Watch the demo →](_DEMO_LINK_HERE_)**

3–5 minute walkthrough showing:
- Hindi voice query → RAG retrieval → spoken response
- English voice query → spoken response
- Escalation flow → WhatsApp notification via n8n

---

## License
MIT — Built for Sarvam AI pre-sales assignment
