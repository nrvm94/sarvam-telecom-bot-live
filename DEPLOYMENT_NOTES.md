# Sarvam Telecom Bot — Deployment Notes

Live URL: https://sarvam-telecom-bot-live-production.up.railway.app
Live GitHub repo: https://github.com/nrvm94/sarvam-telecom-bot-live
Local project: `sarvam-telecom-bot` (never modified — all changes go to live repo only)

---

## What This Document Is For

This file tracks every change made from the local project to produce the Railway-deployed version. When we later migrate these changes back to the main `sarvam-telecom-bot` repo, this document is the complete reference.

---

## Architecture Overview

```
Browser (mic audio)
  │  PCM16 @ 16kHz via WebSocket binary frames
  ▼
Railway FastAPI (backend/main.py)
  │
  ├── /voice/start   → creates call context
  ├── /ws/voice/{id} → voice_pipeline.py (VAD loop)
  │     │
  │     ├── _pcm16_to_wav() → WAV bytes
  │     ├── SarvamClient.transcribe_audio() → STT (saarika:v2.5)
  │     ├── Orchestrator.process_turn() → RAG + LLM (sarvam-105b)
  │     └── SarvamClient.synthesize_speech() → TTS (bulbul:v3)
  │
  ├── /n8n/webhook   → receives n8n callback after escalation
  ├── /mock/*        → mock downstream services (ticket, WhatsApp, etc.)
  └── /*             → serves React SPA (frontend/dist/)

n8n Cloud (escalation path):
  Webhook → Create Ticket (/mock/ticket) → Send WhatsApp (Dialog360) → Callback (/n8n/webhook)
```

---

## Railway Configuration

**File: `railway.json`** (root)
```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "deploy": {
    "startCommand": "cd backend && python -m uvicorn main:app --host 0.0.0.0 --port $PORT",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 3
  }
}
```
No `buildCommand` — frontend `dist/` is committed to the repo.

**File: `.python-version`** (root, NEW)
```
3.11
```
Forces Railway (railpack/Nixpacks) to use Python 3.11 instead of defaulting to 3.13.
Reason: `chroma-hnswlib==0.7.3` (chromadb 0.4.18 dep) has no Python 3.13 binary wheels.

---

## Environment Variables (Railway)

| Variable | Value | Notes |
|----------|-------|-------|
| `SARVAM_API_KEY` | `sk_wduh1s62_...` | Real key from Sarvam dashboard |
| `SARVAM_API_BASE` | `https://api.sarvam.ai` | **No /v1 suffix** — code appends paths itself |
| `SUPABASE_URL` | `https://uidusfqgjise...supabase.co` | From Supabase project settings |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbGci...` | Service role key (not anon key) |
| `DEFAULT_TTS_VOICE` | `female_1` | Maps to `ritu` via VOICE_MAP in sarvam_client.py |
| `DEFAULT_LANGUAGE` | `hi` | Default language for new calls |
| `BACKEND_URL` | `https://sarvam-telecom-bot-live-production.up.railway.app` | Railway domain |
| `FRONTEND_URL` | `https://sarvam-telecom-bot-live-production.up.railway.app` | Same service |
| `MOCK_SERVER_URL` | `https://sarvam-telecom-bot-live-production.up.railway.app` | Mock endpoints are built into main.py |
| `N8N_WEBHOOK_URL` | `https://nrvmhdn.app.n8n.cloud/webhook/escalation` | n8n cloud production webhook |
| `ENVIRONMENT` | `production` | |
| `LOG_LEVEL` | `DEBUG` | |
| `DIALOG_360_API_KEY` | `Z47MFTHQK4...` | Used by n8n, not by the app directly |
| `DIALOG_360_BASE_URL` | `https://waba-sandbox.360dialog.io` | Used by n8n only |

**Critical note on SARVAM_API_BASE:** The Sarvam dashboard shows `https://api.sarvam.ai/v1` for the LLM endpoint. Do NOT use this as the base URL. The code in `sarvam_client.py` builds paths like:
- STT: `base_url + "/speech-to-text"`
- LLM: `base_url + "/v1/chat/completions"`
- TTS: `base_url + "/text-to-speech"`

If base = `.../v1`, LLM becomes `.../v1/v1/chat/completions` (double v1 = 404).

---

## Files Changed vs Local Project

### 1. `requirements.txt` (root) — REPLACED
The root `requirements.txt` is what Railway installs. It was originally `-r backend/requirements.txt` but Railway's build system copies the root file before the full app is available, so the deps must be inline here.

**Before (local):**
```
fastapi==0.104.1
uvicorn==0.24.0
python-dotenv==1.0.0
pydantic==2.5.0
sentence-transformers==2.2.2
chromadb==0.4.18
pipecat-ai[silero]==0.0.51
supabase==2.2.0
python-multipart==0.0.6
aiohttp==3.9.1
aiofiles==23.2.1
```

**After (Railway):**
```
fastapi==0.104.1
uvicorn==0.24.0
python-dotenv==1.0.0
pydantic==2.9.2
chromadb==0.4.18
numpy<2
supabase==2.2.0
python-multipart==0.0.6
aiohttp
```

**What was removed and why:**
- `pipecat-ai[silero]` → pulls `numba~=0.61` → needs `numpy<2.1`, no Python 3.13 wheels, build times out
- `sentence-transformers==2.2.2` → pulls `torch 2.12.0` + 10 CUDA packages (~5GB), Railway free tier disk/memory exceeded
- `aiofiles` → was a pipecat dep, not needed standalone
- `aiohttp==3.9.1` → pinned to unpinned; pipecat required `>=3.11.12` causing backtracking

**What was changed and why:**
- `pydantic==2.5.0` → `2.9.2`: v2.5.0 has no Python 3.13 binary wheels, triggers slow Rust source build
- `numpy<2` added: chromadb 0.4.18 uses `np.float_` which was removed in NumPy 2.0

### 2. `backend/requirements.txt` — REPLACED (mirrors root)
Kept in sync with root `requirements.txt`. Same content.

### 3. `backend/voice_pipeline.py` — NEW FILE
Replaces `pipecat_bot.py` functionality without pipecat dependency.

**What it does:**
- Same WebSocket entry point: `run_voice_ws_pipeline(websocket, call_id, sarvam_client, orchestrator)`
- RMS energy thresholding for VAD (replaces silero neural VAD)
- Sends same JSON message types the frontend expects: `transcript`, `response`, `audio_chunk`, `error`
- Wraps raw PCM16 from browser in WAV container before STT

**Key constants:**
```python
_SPEECH_RMS = 300        # RMS threshold for speech detection
_SILENCE_CHUNKS = 15     # consecutive silent 2048-sample chunks before end-of-utterance
_MIN_SPEECH_CHUNKS = 2   # discard utterances shorter than this (avoids noise blips)
_MAX_AUDIO_BYTES = 30 * 16000 * 2  # 30s hard cap
```

**Critical fix — PCM16 to WAV wrapping:**
Browser sends raw `Int16Array.buffer` (PCM16 at 16kHz, no container). The `transcribe_audio`
method checks for `b'RIFF'` to detect WAV format. Without this header, it falls back to
`audio/webm` content-type → Sarvam rejects with HTTP 400 "Failed to read the file".
Fix: `_pcm16_to_wav()` wraps raw bytes in a proper WAV container (44-byte RIFF header).

### 4. `backend/rag_engine.py` — MODIFIED
Replaced `SentenceTransformer` (which pulls PyTorch) with `DefaultEmbeddingFunction` from chromadb (uses ONNX, already a chromadb dependency, ~23MB model download on first run).

**Before:**
```python
from sentence_transformers import SentenceTransformer
# in __init__:
self.embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
# in _load_documents:
embeddings = [self.embed_model.encode(c).tolist() for c in contents]
self.collection.add(ids=ids, documents=contents, metadatas=metadatas, embeddings=embeddings)
# in query:
results = self.collection.query(query_embeddings=[self.embed_model.encode(question).tolist()], ...)
```

**After:**
```python
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
# in __init__:
self._ef = DefaultEmbeddingFunction()
self.collection = self.chroma_client.get_or_create_collection(
    name="airtel_kb",
    embedding_function=self._ef,
    metadata={"hnsw:space": "cosine"},
)
# in _load_documents:
self.collection.add(ids=ids, documents=contents, metadatas=metadatas)
# chromadb calls self._ef automatically
# in query:
results = self.collection.query(query_texts=[question], ...)
```

### 5. `backend/main.py` — ONE LINE CHANGED
```python
# Before:
from pipecat_bot import run_voice_ws_pipeline

# After:
from voice_pipeline import run_voice_ws_pipeline
```

---

## Issues Encountered During Deployment (Chronological)

| # | Error | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | `pip: command not found` | Railway railpack doesn't run a buildCommand for pip | Removed buildCommand, created root requirements.txt |
| 2 | `backend/requirements.txt not found` | `-r backend/requirements.txt` ran before app was available | Inlined all deps directly in root requirements.txt |
| 3 | `aiofiles` conflict | pipecat-ai[silero] pulls specific aiofiles version | Removed aiofiles pin |
| 4 | `aiohttp` conflict | pipecat-ai requires `>=3.11.12`, pinned `3.9.1` violated this | Changed to unpinned `aiohttp` |
| 5 | Build timeout (pip backtracking forever) | `pipecat-ai[silero]` → `numba~=0.61` → `numpy<2.1` source build on Python 3.13 | Replaced pipecat with `voice_pipeline.py`, removed from requirements |
| 6 | Build timeout (disk/memory exceeded) | `sentence-transformers` → `torch 2.12.0` + 10 CUDA packages (~5GB) | Replaced with `DefaultEmbeddingFunction` in rag_engine.py |
| 7 | pydantic Rust build | `pydantic==2.5.0` has no Python 3.13 wheels, triggers slow Rust compilation | Upgraded to `pydantic==2.9.2` which has 3.13 wheels |
| 8 | `chroma-hnswlib` no wheels | Released Jan 2024 before Python 3.13 existed | Pinned `.python-version: 3.11` |
| 9 | `np.float_ removed` | `chromadb==0.4.18` uses `np.float_` removed in NumPy 2.0 | Pinned `numpy<2` in requirements.txt |
| 10 | STT HTTP 400 "Failed to read file" | Browser sends raw PCM16 bytes; code detected missing RIFF header and sent as `audio/webm` | Added `_pcm16_to_wav()` in voice_pipeline.py to wrap before STT |

---

## Known Bugs in the Original Local Project (to fix when migrating)

### Bug 1: SARVAM_API_BASE `/v1` strip never fires
**File:** `backend/sarvam_client.py`, line ~262
```python
# Bug: \v is vertical tab character, not forward slash
if clean.endswith("\v1"):   # NEVER TRUE
    clean = clean[:-3]
```
Should be:
```python
if clean.endswith("/v1"):
    clean = clean[:-3]
```
Impact: If `SARVAM_API_BASE` is set to `https://api.sarvam.ai/v1` (as the Sarvam dashboard suggests), all API calls break. Workaround: set env var to `https://api.sarvam.ai` (no /v1).

### Bug 2: 450-char TTS truncation cuts off responses (especially Marathi)
**File:** `backend/voice_pipeline.py`, `_process()` function
```python
# This block is harmful — sarvam_client.synthesize_speech() already handles
# splitting long text into ≤500-char chunks internally. This truncation
# causes Marathi (longer Devanagari sentences) responses to be cut off mid-speech.
if len(tts_text) > 450:
    parts = re.split(r"(?<=[।.!?])\s+", tts_text)
    truncated, acc = [], 0
    for p in parts:
        if acc + len(p) > 450:
            break
        truncated.append(p)
        acc += len(p) + 1
    tts_text = " ".join(truncated) if truncated else tts_text[:450]
```
Fix: Remove this entire block. `synthesize_speech` handles chunking internally.

---

## Current Performance Issues

### Latency (STT + LLM + TTS turnaround)
**Cause:** Railway deploys to US West by default. User is in India. Sarvam API is India-based.
Each API call travels India→US→India (or US→India→US), adding ~300-400ms per call.
With 3 sequential calls: easily 1-2 seconds extra vs local.

**Fix applied:** Changed Railway region to `ap-southeast-1` (Singapore) via Railway dashboard → Service → Settings → Region. Confirmed latency improvement.

---

## n8n Cloud Escalation Setup — COMPLETE

**Workflow:** `https://nrvmhdn.app.n8n.cloud` (account: nrvm94)
**Webhook URL set in Railway:** `https://nrvmhdn.app.n8n.cloud/webhook/escalation`

4-node workflow (active):

1. **Webhook Trigger** — POST, path `escalation`
   Production URL: `https://nrvmhdn.app.n8n.cloud/webhook/escalation`

2. **HTTP Request — Create Ticket**
   - Method: POST
   - URL: `https://sarvam-telecom-bot-live-production.up.railway.app/mock/ticket`
   (Mock endpoints are built into the FastAPI app — no separate mock_server.py needed)

3. **HTTP Request — Send WhatsApp** (Dialog360)
   - URL: `https://waba-sandbox.360dialog.io/v1/messages`
   - Header: `D360-API-KEY: Z47MFTHQK4QUB1O7GFMD2UOP6TPYL643`
   - Body Content Type: JSON (NOT JSON.stringify — n8n serialises automatically)
   ```json
   {
     "messaging_product": "whatsapp",
     "recipient_type": "individual",
     "to": "91XXXXXXXXXX",
     "type": "text",
     "text": { "body": "={{ 'Escalation raised. Ticket: ' + $json.ticket_id }}" }
   }
   ```
   - No emojis or em-dashes in n8n expressions (cause parser errors)
   - WhatsApp messages arrive in **Updates tab** (not main Chats tab)
   - Dialog360 sandbox requires periodic opt-in: send any WhatsApp from `91XXXXXXXXXX` to the sandbox number shown in 360dialog dashboard

4. **HTTP Request — Callback**
   - Method: POST
   - URL: `https://sarvam-telecom-bot-live-production.up.railway.app/n8n/webhook`
   - Body:
   ```json
   {
     "call_id": "={{ $('Webhook').item.json.call_id }}",
     "ticket_id": "={{ $('Create Ticket').item.json.ticket_id }}",
     "status": "escalated"
   }
   ```

**For main project migration:** The n8n workflow Create Ticket URL and Callback URL must be updated to point to the new deployment domain. WhatsApp node stays the same.

---

## Rollback Instructions

Tag `v1-working-baseline` = commit `dddf776` is the last known fully working state.

```bash
# To roll back locally:
git checkout v1-working-baseline

# To roll back Railway deployment (force push the tag as main):
git checkout -b rollback-branch v1-working-baseline
git push origin rollback-branch:main --force
```

---

## Migration Plan: Applying All Changes to Main Project (`sarvam-telecom-bot`)

Apply these changes in order. Each step is self-contained.

### Step 1 — Copy new file: `backend/voice_pipeline.py`
Copy `backend/voice_pipeline.py` from live repo verbatim.
This replaces pipecat entirely — no pipecat import, no silero, no torch.

### Step 2 — Modify `backend/main.py` (1 line)
```python
# Change:
from pipecat_bot import run_voice_ws_pipeline
# To:
from voice_pipeline import run_voice_ws_pipeline
```

### Step 3 — Modify `backend/rag_engine.py`
Replace the SentenceTransformer embedding with chromadb's built-in ONNX embedder.

Change imports at top:
```python
# Remove:
from sentence_transformers import SentenceTransformer
# Add:
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
```

In `__init__`, replace embed model setup:
```python
# Remove:
self.embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

# Add (after chroma_client is created):
self._ef = DefaultEmbeddingFunction()
```

In `get_or_create_collection` call, add embedding_function:
```python
self.collection = self.chroma_client.get_or_create_collection(
    name="airtel_kb",
    embedding_function=self._ef,      # add this line
    metadata={"hnsw:space": "cosine"},
)
```

In `_load_documents`, remove manual embed calls:
```python
# Remove these lines:
embeddings = [self.embed_model.encode(c).tolist() for c in contents]
# and the embeddings= kwarg in collection.add()

# collection.add() becomes:
self.collection.add(ids=ids, documents=contents, metadatas=metadatas)
```

In `query`, replace query_embeddings with query_texts:
```python
# Remove:
results = self.collection.query(query_embeddings=[self.embed_model.encode(question).tolist()], ...)
# Change to:
results = self.collection.query(query_texts=[question], ...)
```

### Step 4 — Fix `backend/sarvam_client.py` `/v1` strip bug (~line 262)
```python
# Change:
if clean.endswith("\v1"):
# To:
if clean.endswith("/v1"):
```

### Step 5 — Update `backend/requirements.txt`
```
# Remove:
sentence-transformers==2.2.2
pipecat-ai[silero]==0.0.51
aiofiles==23.2.1

# Change:
pydantic==2.5.0  →  pydantic==2.9.2
aiohttp==3.9.1   →  aiohttp

# Add:
numpy<2
```

### Step 6 — Add `.python-version` (root)
Create file `.python-version` with content:
```
3.11
```

### Step 7 — Update `.env`
```
SARVAM_API_BASE=https://api.sarvam.ai
```
(Remove the `/v1` suffix if present — the code appends it per-endpoint.)

### Step 8 — Update n8n local workflow (for local testing)
The local n8n Docker workflow's "Create Ticket" node should point to `http://localhost:8000/mock/ticket`
instead of `http://localhost:5000/...` since mock_server.py is no longer needed.
The Callback node should point to `http://localhost:8000/n8n/webhook`.

### Step 9 — Delete `backend/pipecat_bot.py`
No longer needed. `voice_pipeline.py` replaces it entirely.

---

## Current Status — All Features Working

| Feature | Status | Notes |
|---------|--------|-------|
| Voice call (STT → LLM → TTS) | Working | Hindi, English, Marathi |
| RAG knowledge base | Working | chromadb ONNX embeddings |
| Escalation detection | Working | "escalate" in EN/Hindi/Devanagari |
| n8n cloud webhook | Working | `https://nrvmhdn.app.n8n.cloud/webhook/escalation` |
| WhatsApp notification | Working | Dialog360 sandbox → Updates tab |
| Supabase logging | Working | Env vars configured |
| Mock downstream services | Working | Built into FastAPI at `/mock/*` |
| Latency | Acceptable | Railway Singapore region |
| Marathi TTS full narration | Working | 450-char truncation removed |

---

## Git History (Live Repo — Key Commits)

| Commit | Description |
|--------|-------------|
| `baeead9` | fix: remove 450-char TTS truncation; add deployment notes |
| `dddf776` | **v1-working-baseline** — fix: wrap raw PCM16 in WAV before STT |
| `cc3fdeb` | fix: pin numpy<2 (chromadb 0.4.18 / NumPy 2.0 incompatibility) |
| `355576c` | fix: pin Python to 3.11 via .python-version |
| `16cfdf6` | fix: replace sentence-transformers with chromadb ONNX, upgrade pydantic |
| `6a6743e` | fix: replace pipecat with voice_pipeline.py (RMS VAD) |

---

*Last updated: 2026-06-15 — Status: Fully deployed and working on Railway. Next: migrate all changes to main project repo (`sarvam-telecom-bot`).*
