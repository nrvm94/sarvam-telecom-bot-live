# Sarvam Telecom Bot — Project Notes for Claude

## Sarvam AI API — Known Constraints

### TTS (bulbul:v3)
- **Model in use:** `bulbul:v3`
- **Speaker names:** Must use v3-compatible names. v2 names (`anushka`, `manisha`, `vidya`, `abhilash`, `karun`, `hitesh`) are NOT valid for v3 and cause HTTP 400.
  - Current mapping (in `backend/sarvam_client.py` → `VOICE_MAP`):
    - `female_1` → `ritu`
    - `female_2` → `priya`
    - `female_3` → `neha`
    - `male_1` → `rahul`
    - `male_2` → `amit`
    - `male_3` → `aditya`
  - Full v3 speaker list: aditya, ritu, ashutosh, priya, neha, rahul, pooja, rohan, simran, kavya, amit, dev, ishita, shreya, ratan, varun, manan, sumit, roopa, kabir, aayan, shubh, advait, anand, tanya, tarun, sunny, mani, gokul, vijay, shruti, suhani, mohit, kavitha, rehan, soham, rupali, niharika
- **`loudness` parameter:** NOT supported in bulbul:v3 — causes HTTP 400. Do not pass it.
- **`pace` parameter:** Valid range 0.5–2.0. Default/natural speed = `1.0`.
- **`speech_sample_rate`:** `8000` (telephony quality, matches use case).
- **`enable_preprocessing`:** `true` (handles abbreviations, numbers, etc.)

### STT (saarika:v2.5)
- Audio sent as `multipart/form-data` with field name `file`.
- Auth header: `api-subscription-key` (NOT Bearer).
- Language code: `hi-IN` or `en-IN`.

### LLM (sarvam-30b)
- Auth header: `Authorization: Bearer <key>` (OpenAI-compatible endpoint).
- Do NOT set `max_tokens` — the model uses reasoning tokens internally and a hard cap truncates the response.

## How to Run

**All 4 processes must be running** for the full escalation + WhatsApp flow to work:

| Process | Command | Port | Notes |
|---------|---------|------|-------|
| n8n | `docker start n8n` | 5678 | Start Docker first |
| Backend | `python -m uvicorn main:app --reload --port 8000` (in `backend/`) | 8000 | Main FastAPI app |
| Mock server | `python mock_server.py` (in `backend/`) | 5000 | Required by n8n's "Create Ticket" node — if this is down, n8n workflow stops before sending WhatsApp |
| Frontend | `npm run dev` (in `frontend/`) | 3000 | Vite proxies `/voice/*`, `/health`, `/n8n` to port 8000 |

### Why mock_server.py is critical
The n8n escalation workflow is: `Webhook → Set Fields → Create Ticket (port 5000) → Send WhatsApp → Callback`
If mock_server.py is not running, the "Create Ticket" step fails and WhatsApp is **never sent**.

## Escalation & WhatsApp (n8n + Dialog360)

### How it works
`Customer says "escalate"` → backend detects via `EscalationAgent.should_escalate()` → POSTs to n8n webhook → n8n: Create Ticket (port 5000) → Send WhatsApp (Dialog360) → Callback to FastAPI

### Escalation detection
- `should_escalate()` in `backend/agents.py` must be `async def` — orchestrator awaits it.
- Phrase matching checks English, Roman Hindi, AND Devanagari lists (`ESCALATION_PHRASES` in `agents.py`).
- STT often outputs Devanagari for Hindi/code-switched speech (e.g. "एस्केलेट" not "escalate") — the Devanagari list handles this.

### n8n Send WhatsApp node
- Body Content Type: **JSON**, Specify Body: **Using JSON**
- Do NOT wrap the body in `JSON.stringify()` — n8n serialises it automatically.
- Use `={{ expression }}` only inside the JSON for dynamic values, e.g.:
  ```json
  {
    "messaging_product": "whatsapp",
    "recipient_type": "individual",
    "to": "91XXXXXXXXXX",
    "type": "text",
    "text": {
      "body": "={{ 'Ticket: ' + $json.ticket_id + '\\nCustomer: ' + $json.customer_name }}"
    }
  }
  ```
- Emoji and em dash (`—`) in expressions cause n8n parser syntax errors — avoid them.

### Dialog360 sandbox gotcha
- Even if the API returns HTTP 200 + a `wamid` message ID, the message is silently dropped if the recipient number has not opted in.
- **To opt in:** send any WhatsApp message from `91XXXXXXXXXX` to the 360dialog sandbox number (visible in the 360dialog dashboard). Must be re-done if the sandbox session expires.
- Messages land in WhatsApp → **Updates tab** (Business section), not the main Chats tab.

## Key Files
- `backend/sarvam_client.py` — All Sarvam API calls (STT, LLM, TTS). VOICE_MAP and TTS payload live here.
- `backend/main.py` — FastAPI routes and pipeline.
- `backend/orchestrator.py` — Multi-agent pipeline (RAG → LLM → escalation).
- `.env` — API keys and config. `DEFAULT_TTS_VOICE=female_1` controls the speaker.
