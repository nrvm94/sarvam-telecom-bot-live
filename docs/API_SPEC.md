# API Specification — Sarvam Telecom Bot

Base URL: `http://localhost:8000`

All requests and responses use `Content-Type: application/json`.

---

## GET /health

Health check endpoint. Returns service status and current timestamp.

### Response Schema
```json
{
  "status": "string",        // "ok"
  "service": "string",       // "Sarvam Telecom Bot"
  "timestamp": "string"      // ISO 8601 UTC timestamp
}
```

### Example
```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "service": "Sarvam Telecom Bot",
  "timestamp": "2024-01-15T10:30:00.000000+00:00"
}
```

---

## POST /voice/start

Initiate a new voice call session. Returns a unique call_id for subsequent requests.

### Request Body
```json
{
  "customer_phone": "string",   // optional, customer's phone number
  "customer_name": "string",    // optional, customer's name
  "language": "string"          // "hi" (default) or "en"
}
```

### Response Schema
```json
{
  "call_id": "string",      // unique call identifier e.g. "call_a1b2c3d4e5f6"
  "status": "string",       // "initiated"
  "timestamp": "string"     // ISO 8601 UTC timestamp
}
```

### Example
```bash
curl -X POST http://localhost:8000/voice/start \
  -H "Content-Type: application/json" \
  -d '{
    "customer_phone": "9876543210",
    "customer_name": "Rajesh Kumar",
    "language": "hi"
  }'
```

```json
{
  "call_id": "call_a1b2c3d4e5f6",
  "status": "initiated",
  "timestamp": "2024-01-15T10:30:01.123456+00:00"
}
```

---

## POST /voice/transcribe

Main voice pipeline endpoint. Accepts audio, runs STT → RAG → LLM → TTS, and returns both text and audio response.

### Request Body
```json
{
  "audio_base64": "string",  // base64-encoded WebM/WAV audio from browser
  "call_id": "string",       // call_id from /voice/start
  "language": "string"       // "hi" or "en"
}
```

### Response Schema (200 OK)
```json
{
  "transcription": "string",   // customer's speech converted to text
  "response": "string",        // bot's text response
  "audio_base64": "string",    // base64-encoded WAV audio of bot's response
  "language": "string",        // language used ("hi" or "en")
  "escalate": "boolean",       // true if complex issue detected
  "issue_type": "string",      // classified issue category
  "timestamp": "string"        // ISO 8601 UTC timestamp
}
```

### Issue Types
| Value | Description |
|-------|-------------|
| `balance_check` | Customer asking about account balance |
| `plan_query` | Questions about plans, offers, validity |
| `data_query` | Internet/data balance or speed issues |
| `billing_query` | Bill payment or invoice questions |
| `billing_dispute` | Disputed charges or refund requests |
| `port_request` | Mobile number portability request |
| `general_query` | Any other query |

### Response Schema (500 Error)
```json
{
  "error": "string",    // technical error message
  "detail": "string",   // user-friendly error description
  "timestamp": "string"
}
```

### Example
```bash
# First encode an audio file to base64
AUDIO_B64=$(base64 -w 0 test_audio.webm)

curl -X POST http://localhost:8000/voice/transcribe \
  -H "Content-Type: application/json" \
  -d "{
    \"audio_base64\": \"$AUDIO_B64\",
    \"call_id\": \"call_a1b2c3d4e5f6\",
    \"language\": \"hi\"
  }"
```

```json
{
  "transcription": "mera balance kitna hai",
  "response": "Aapka balance check karne ke liye *121# dial karein ya MyAirtel app open karein. App mein Home screen par aapka balance aur data automatically dikhega.",
  "audio_base64": "UklGRiQAAABXQVZFZm10IBAAAA...",
  "language": "hi",
  "escalate": false,
  "issue_type": "balance_check",
  "timestamp": "2024-01-15T10:30:05.456789+00:00"
}
```

---

## POST /voice/end

End an active call session. Records duration and marks call as completed in Supabase.

### Request Body
```json
{
  "call_id": "string",          // call_id from /voice/start
  "duration_seconds": "integer" // total call duration in seconds
}
```

### Response Schema
```json
{
  "call_id": "string",
  "status": "string",           // "completed"
  "duration_seconds": "integer",
  "timestamp": "string"
}
```

### Example
```bash
curl -X POST http://localhost:8000/voice/end \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "call_a1b2c3d4e5f6",
    "duration_seconds": 127
  }'
```

```json
{
  "call_id": "call_a1b2c3d4e5f6",
  "status": "completed",
  "duration_seconds": 127,
  "timestamp": "2024-01-15T10:32:08.789012+00:00"
}
```

---

## POST /n8n/webhook

Receives callback from n8n after escalation workflow completion. Updates call record with ticket information.

### Request Body (from n8n)
```json
{
  "call_id": "string",    // call identifier
  "ticket_id": "string",  // created support ticket ID e.g. "TKT-58291"
  "status": "string"      // escalation status e.g. "escalated"
}
```

### Response Schema
```json
{
  "status": "string",    // "received"
  "timestamp": "string"
}
```

### Example (simulating n8n callback)
```bash
curl -X POST http://localhost:8000/n8n/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "call_a1b2c3d4e5f6",
    "ticket_id": "TKT-58291",
    "status": "escalated"
  }'
```

```json
{
  "status": "received",
  "timestamp": "2024-01-15T10:30:08.123456+00:00"
}
```

---

## Mock Server Endpoints (port 5000)

### POST /mock/ticket
```bash
curl -X POST http://localhost:5000/mock/ticket
```
```json
{
  "ticket_id": "TKT-58291",
  "status": "created",
  "timestamp": "2024-01-15T10:30:06.000000+00:00"
}
```

### POST /mock/sms
```bash
curl -X POST http://localhost:5000/mock/sms
```
```json
{
  "status": "sent",
  "message": "SMS delivered"
}
```

### POST /mock/whatsapp
```bash
curl -X POST http://localhost:5000/mock/whatsapp
```
```json
{
  "status": "sent",
  "message": "WhatsApp delivered"
}
```
