# System Architecture — Sarvam Telecom Voice Bot

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        CUSTOMER BROWSER                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  React Frontend (Vite + TailwindCSS) — localhost:3000   │   │
│  │  • WebRTC Microphone capture (MediaRecorder API)        │   │
│  │  • Web Audio API for TTS playback                       │   │
│  │  • Conversation history UI                              │   │
│  └────────────────────┬────────────────────────────────────┘   │
└───────────────────────┼─────────────────────────────────────────┘
                        │  HTTP/JSON (audio as base64)
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│              FastAPI Backend — localhost:8000                   │
│                                                                  │
│  POST /voice/start      → Create call session                   │
│  POST /voice/transcribe → Full pipeline (STT→RAG→LLM→TTS)     │
│  POST /voice/end        → End session                           │
│  POST /n8n/webhook      → Receive escalation callback          │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │SarvamClient  │  │AirtelKB (RAG)│  │ConversationManager   │  │
│  │• STT (Saaras)│  │• ChromaDB    │  │• detect_escalation() │  │
│  │• LLM         │  │• MiniLM-L6v2 │  │• classify_issue()    │  │
│  │• TTS (Bulbul)│  │• 21 KB docs  │  └──────────────────────┘  │
│  └──────┬───────┘  └──────────────┘                            │
└─────────┼──────────────────────────────────────────────────────┘
          │                    │                      │
          ▼                    ▼                      ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐
│   SARVAM AI APIs │ │   CHROMADB       │ │   SUPABASE           │
│  (api.sarvam.ai) │ │  (local SQLite)  │ │  (PostgreSQL cloud)  │
│                  │ │                  │ │                      │
│ /speech-to-text  │ │ Vector store for │ │  Table: calls        │
│   ↑ 300ms STT    │ │ knowledge base   │ │  • call_id           │
│                  │ │ documents        │ │  • conversation[]    │
│ /chat/completions│ │                  │ │  • ticket_id         │
│   ↑ 1-2s LLM     │ │ cosine distance  │ │  • escalated         │
│                  │ │ top-3 retrieval  │ │  • duration_seconds  │
│ /text-to-speech  │ └──────────────────┘ └──────────────────────┘
│   ↑ 500ms TTS    │
└──────────────────┘
          │ escalate=True
          ▼
┌──────────────────────────────────────────────────────────────────┐
│               N8N AUTOMATION — localhost:5678                    │
│                                                                   │
│  Webhook Trigger                                                  │
│       ↓                                                           │
│  Set Fields (extract call_id, issue_type, user_query)            │
│       ↓                                                           │
│  HTTP: Create Ticket → localhost:5000/mock/ticket                 │
│       ↓                                                           │
│  HTTP: Send WhatsApp → 360dialog sandbox API                     │
│       ↓                                                           │
│  HTTP: Callback → localhost:8000/n8n/webhook                      │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────┐  ┌──────────────────────────────────────────┐
│  MOCK SERVER     │  │  360DIALOG (WhatsApp Business API)       │
│  localhost:5000  │  │  waba-sandbox.360dialog.io               │
│                  │  │                                          │
│ POST /mock/ticket│  │  Send escalation notification to         │
│ POST /mock/sms   │  │  customer's WhatsApp number             │
│ POST /mock/whats │  └──────────────────────────────────────────┘
└──────────────────┘
```

---

## Component Descriptions

| Component | Technology | Purpose | Cost Model |
|-----------|-----------|---------|------------|
| Frontend | React 18 + Vite + TailwindCSS | Voice capture UI, audio playback, conversation display | Free / Open source |
| Backend API | FastAPI + Python 3.11 + uvicorn | Orchestrates all services, handles HTTP lifecycle | ~₹2,000/month (cloud VM) |
| STT | Sarvam Saaras v3 (saaras:v3) | Hindi/English + auto language detection, 300ms latency | ₹0.18/minute |
| LLM | Sarvam sarvam-105b | Reasoning model; response generation with Airtel context | ₹0.05/1K tokens |
| TTS | Sarvam Bulbul v3 (bulbul:v3) | 37+ Indian voices; natural Hindi/English synthesis | ₹0.10/1K chars |
| Vector Store | ChromaDB (local SQLite) | Semantic search over Airtel knowledge base | Free (local) |
| Embeddings | sentence-transformers/all-MiniLM-L6-v2 | Document and query embedding | Free (local model) |
| Database | Supabase (PostgreSQL) | Call logs, conversation history, escalation tracking | Free tier / $25/month |
| Automation | n8n (Docker) | Escalation workflow orchestration | Free (self-hosted) |
| WhatsApp | 360dialog Business API | Customer notifications | ₹0.50/message |
| Mock Server | FastAPI (port 5000) | Simulates ticket system for testing | Free |

---

## API Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | /health | Service health check | None |
| POST | /voice/start | Initiate a new call session | None |
| POST | /voice/transcribe | Full STT→RAG→LLM→TTS pipeline | None |
| POST | /voice/end | End call and record duration | None |
| POST | /n8n/webhook | Receive n8n escalation callback | None |

---

## Data Flow: Happy Path (No Escalation)

```
1. User opens browser → http://localhost:3000
   └─ React app loads, microphone permission requested

2. User clicks "Start Call"
   └─ POST /voice/start → {call_id: "call_abc123...", status: "initiated"}
   └─ call_id stored in React state
   └─ Supabase: INSERT INTO calls (call_id, language, started_at)

3. User clicks Mic → speaks in Hindi: "Mera balance kitna hai?"
   └─ MediaRecorder captures audio as WebM/Opus
   └─ Audio encoded to base64

4. User clicks Mic again to stop
   └─ POST /voice/transcribe {audio_base64, call_id, language: "hi"}

5. Backend pipeline (sequential):
   a. base64 → audio_bytes (decoded)
   b. Sarvam STT: audio_bytes → "mera balance kitna hai" [300ms]
   c. ChromaDB query: "mera balance kitna hai" → [balance_check docs] [50ms]
   d. Sarvam LLM: query + context → "Aapka balance check karne ke liye..." [1.5s]
   e. Escalation check: "balance" keyword → False
   f. Issue classify: "balance" → "balance_check"
   g. Sarvam TTS: response text → audio_bytes [500ms]
   h. audio_bytes → base64
   i. Supabase: UPDATE calls SET conversation = [...turns]

6. Response returned to frontend:
   └─ Transcription displayed in UI
   └─ Bot response displayed in UI
   └─ Audio played via Web Audio API
   └─ Conversation history updated

7. Total round-trip time: ~2.5–4 seconds
```

---

## Data Flow: Escalation Path

```
1–5. Same as happy path above

5b. User says: "Mujhe galat charge aaya hai, refund chahiye"
    └─ Escalation check: "galat charge", "refund" → True
    └─ Issue classify: "galat" → "billing_dispute"

5c. trigger_n8n_escalation() fires (non-blocking):
    └─ POST http://localhost:5678/webhook/escalation
       {call_id, issue_type: "billing_dispute", user_query, bot_response}

6. n8n workflow executes:
   a. Webhook receives payload
   b. Set Fields extracts call_id, issue_type
   c. POST http://localhost:5000/mock/ticket → {ticket_id: "TKT-58291"}
   d. POST https://waba-sandbox.360dialog.io/v1/messages → WhatsApp sent
   e. POST http://localhost:8000/n8n/webhook → {call_id, ticket_id, status: "escalated"}

7. FastAPI /n8n/webhook:
   └─ supabase.update_escalation(call_id, ticket_id, "escalated")
   └─ Supabase: UPDATE calls SET ticket_id="TKT-58291", escalated=true

8. Frontend:
   └─ Response includes escalate: true
   └─ Yellow escalation banner shown
   └─ "Your issue has been escalated. Agent will contact you in 2 hours."
```

---

## Security Considerations

1. **API Keys:** All credentials stored in `.env` (gitignored) — never hardcoded
2. **CORS:** Restricted to localhost:3000 in development; must be locked to production domain in prod
3. **Input validation:** Pydantic models validate all incoming request bodies
4. **Error handling:** All exceptions caught; no stack traces exposed to frontend
5. **Audio data:** Base64 audio is transient — not stored, only passed through the pipeline
6. **Supabase RLS:** Row-level security should be enabled in production for multi-tenant scenarios
