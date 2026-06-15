"""
Sarvam Telecom Bot — FastAPI Backend
Orchestrates: STT → RAG → LLM → TTS → Supabase → n8n escalation
"""

import base64
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import re

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---- Load environment first, before importing local modules ---------------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from conversation import ConversationManager        # noqa: E402
from rag_engine import AirtelKnowledgeBase         # noqa: E402
from sarvam_client import SarvamClient             # noqa: E402
from supabase_client import SupabaseClient         # noqa: E402


# ---- Logging setup --------------------------------------------------------
log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---- FastAPI app ----------------------------------------------------------
app = FastAPI(
    title="Sarvam Telecom Bot API",
    description="AI-powered voice support bot for Airtel using Sarvam AI",
    version="1.0.0",
)

# ---- CORS ----------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Request logging middleware ------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    logger.info("→ %s %s", request.method, request.url.path)
    response = await call_next(request)
    duration_ms = (time.time() - start) * 1000
    logger.info(
        "← %s %s | %d | %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# ---- Service clients (lazy, initialised in startup) ----------------------
sarvam_client: SarvamClient = None        # type: ignore[assignment]
rag_engine: AirtelKnowledgeBase = None    # type: ignore[assignment]
conversation_manager: ConversationManager = None  # type: ignore[assignment]
supabase_client: SupabaseClient = None    # type: ignore[assignment]
orchestrator = None                        # type: ignore[assignment]


# ---- Startup event -------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    global sarvam_client, rag_engine, conversation_manager, supabase_client, orchestrator  # noqa: PLW0603

    # Log config (mask secrets)
    logger.info("=" * 60)
    logger.info("Sarvam Telecom Bot starting up ...")
    logger.info("ENVIRONMENT   : %s", os.getenv("ENVIRONMENT", "development"))
    logger.info("LOG_LEVEL     : %s", os.getenv("LOG_LEVEL", "DEBUG"))
    logger.info("BACKEND_URL   : %s", os.getenv("BACKEND_URL"))
    logger.info("FRONTEND_URL  : %s", os.getenv("FRONTEND_URL"))
    logger.info("N8N_WEBHOOK   : %s", os.getenv("N8N_WEBHOOK_URL"))
    api_key = os.getenv("SARVAM_API_KEY", "")
    logger.info("SARVAM_KEY    : %s***", api_key[:8] if api_key else "MISSING")
    logger.info("SUPABASE_URL  : %s", os.getenv("SUPABASE_URL", "NOT SET"))
    logger.info("DEMO_DEFAULT  : %s", os.getenv("DEMO_DEFAULT_PHONE", "NOT SET"))
    logger.info("=" * 60)

    # Initialise clients
    sarvam_client = SarvamClient(
        api_key=os.getenv("SARVAM_API_KEY", ""),
        base_url=os.getenv("SARVAM_API_BASE", "https://api.sarvam.ai"),
    )

    rag_engine = AirtelKnowledgeBase()

    conversation_manager = ConversationManager()

    supabase_client = SupabaseClient(
        url=os.getenv("SUPABASE_URL", ""),
        key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
    )

    from orchestrator import VoiceOrchestrator
    orchestrator = VoiceOrchestrator(sarvam_client, rag_engine, supabase_client)

    logger.info("Sarvam Telecom Bot started successfully ✓")


# ---- Pydantic request/response models ------------------------------------

class StartCallRequest(BaseModel):
    customer_phone: str = Field(default="", description="Customer phone number (optional)")
    customer_name: str = Field(default="", description="Customer name (optional)")
    language: str = Field(default="hi", description="Language: 'hi' for Hindi, 'en' for English, 'mr' for Marathi")


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(..., description="Base64-encoded audio (WebM or WAV)")
    call_id: str = Field(..., description="Active call identifier")
    language: str = Field(default="hi", description="Language preference: hi, en, or mr")


class EndCallRequest(BaseModel):
    call_id: str = Field(..., description="Active call identifier")
    duration_seconds: int = Field(default=0, description="Total call duration in seconds")


# ---- Routes --------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Health check endpoint — returns service status."""
    return {
        "status": "ok",
        "service": "Sarvam Telecom Bot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/debug/test")
async def debug_test_apis():
    """
    Tests each Sarvam API independently.
    Visit http://localhost:8000/debug/test in browser to diagnose failures.
    Returns pass/fail + error details for each service.
    """
    results = {}

    # 1. LLM test
    try:
        resp = await sarvam_client.generate_response(
            "What is Airtel balance check code?", "", "en"
        )
        results["llm"] = {"status": "ok", "response_preview": resp[:80]}
    except Exception as e:
        results["llm"] = {"status": "error", "error": str(e)}

    # 2. TTS test
    try:
        audio = await sarvam_client.synthesize_speech(
            "Hello this is a test", "en-IN", "female_1"
        )
        results["tts"] = {"status": "ok", "audio_bytes": len(audio)}
    except Exception as e:
        results["tts"] = {"status": "error", "error": str(e)}

    # 3. STT test (with synthetic silent WAV)
    try:
        import wave, io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00" * 32000)  # 1 second silence
        transcript, _lang = await sarvam_client.transcribe_audio(buf.getvalue())
        results["stt"] = {"status": "ok", "transcript": repr(transcript)}
    except Exception as e:
        results["stt"] = {"status": "error", "error": str(e)}

    # 4. Config info (proves which code version is running)
    results["config"] = {
        "stt_model": "saaras:v3",
        "llm_model": "sarvam-105b",
        "tts_model": "bulbul:v3",
        "tts_speaker": "ritu",
        "sarvam_base": os.getenv("SARVAM_API_BASE", ""),
        "key_prefix": os.getenv("SARVAM_API_KEY", "")[:8] + "***",
    }

    all_ok = all(v.get("status") == "ok" for k, v in results.items() if k != "config")
    return {"overall": "ok" if all_ok else "degraded", "tests": results}


@app.get("/debug/customer/{phone}")
async def debug_customer(phone: str):
    """Verify the customer DB lookup in isolation (no voice/STT).
    Visit http://localhost:8000/debug/customer/9876543210 in a browser."""
    from agents import CustomerProfileAgent
    c = CustomerProfileAgent().load_customer(phone)
    found = c.get("segment") != "unknown"
    return {
        "phone": phone,
        "found": found,
        "name": c.get("name"),
        "segment": c.get("segment"),
    }


@app.post("/voice/start")
async def start_call(req: StartCallRequest):
    """
    Initiate a new voice call session.
    Loads customer profile, generates personalised greeting, returns greeting audio.
    """
    call_id = "call_" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    logger.info(
        "New call started | call_id=%s | language=%s | phone=%s",
        call_id,
        req.language,
        req.customer_phone or "unknown",
    )

    # Persist call record to Supabase
    call_data = {
        "call_id": call_id,
        "customer_phone": req.customer_phone,
        "customer_name": req.customer_name,
        "language": req.language,
        "status": "active",
        "started_at": now,
        "conversation": [],
    }
    await supabase_client.insert_call(call_data)

    # Orchestrator: load customer profile and generate personalised greeting
    start_result = await orchestrator.start_call(call_id, req.customer_phone, req.language)

    lang_code = {"hi": "hi-IN", "mr": "mr-IN"}.get(req.language, "en-IN")
    try:
        greeting_audio = await sarvam_client.synthesize_speech(
            text=start_result["greeting"],
            language=lang_code,
            voice=os.getenv("DEFAULT_TTS_VOICE", "female_1"),
        )
        greeting_audio_b64 = base64.b64encode(greeting_audio).decode("utf-8")
    except Exception as e:
        logger.warning("Greeting TTS failed: %s", e)
        greeting_audio_b64 = ""

    return {
        "call_id": call_id,
        "greeting": start_result["greeting"],
        "greeting_audio_base64": greeting_audio_b64,
        "customer_name": start_result["customer_name"],
        "customer_found": start_result["customer_found"],
        "customer_segment": start_result["customer_segment"],
        "looked_up_phone": start_result.get("looked_up_phone", ""),
        "status": "initiated",
        "timestamp": now,
    }


@app.post("/voice/transcribe")
async def transcribe_and_respond(req: TranscribeRequest):
    """
    Full voice pipeline:
      audio → STT → RAG → LLM → TTS → (escalation check) → response
    """
    logger.info(
        "Transcribe request | call_id=%s | language=%s | audio_b64_len=%d",
        req.call_id,
        req.language,
        len(req.audio_base64),
    )

    # Track which pipeline step we're on for precise error reporting
    step = "initialising"

    try:
        # Step 1 — Decode audio
        step = "Step 1: decode audio"
        audio_bytes = base64.b64decode(req.audio_base64)
        logger.debug("Step 1 — Audio decoded | bytes=%d", len(audio_bytes))

        # Step 2 — STT with Saaras v3 (auto language detection)
        step = "Step 2: STT (Sarvam Saaras v3)"
        transcription, detected_lang_code = await sarvam_client.transcribe_audio(audio_bytes)
        transcription = (transcription or "").strip()
        _lang_bcp_map = {"hi-IN": "hi", "mr-IN": "mr", "en-IN": "en"}
        detected_lang = _lang_bcp_map.get(
            detected_lang_code,
            detected_lang_code.split("-")[0] if detected_lang_code else req.language
        )
        logger.info("Step 2 — STT done | saaras=%s | detected=%s | transcript=%r",
                    detected_lang_code, detected_lang, transcription[:100])

        logger.info(
            "Language detected | detected_lang=%s | user_pref=%s | transcription=%r",
            detected_lang, req.language, transcription[:100],
        )

        # Empty transcript guard — silence or background noise
        if not transcription or not transcription.strip():
            logger.info("Empty transcript — returning early without LLM/TTS call")
            silence_msg = {
                "hi": "माफ़ करें, मुझे समझ नहीं आया। क्या आप दोबारा बोल सकते हैं?",
                "mr": "माफ करा, मला समजले नाही. कृपया पुन्हा सांगा.",
                "en": "Sorry, I couldn't hear you clearly. Could you please repeat that?",
            }.get(req.language, "माफ़ करें, मुझे समझ नहीं आया। क्या आप दोबारा बोल सकते हैं?")
            return {
                "transcription": "",
                "response": silence_msg,
                "audio_base64": "",
                "language": req.language,
                "escalate": False,
                "issue_type": "general_query",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Steps 3-6 — Orchestrator (RAG + LLM + escalation + classification)
        step = "Step 3-6: orchestrator process_turn"
        result = await orchestrator.process_turn(req.call_id, transcription, detected_lang)
        bot_response = result["response"]
        escalate = result["escalated"]
        issue_type = result["issue_type"]
        logger.info("Orchestrator done | escalated=%s | issue=%s | response=%r", escalate, issue_type, bot_response[:80])

        # Step 7 — TTS: use detected language for response
        step = "Step 7: TTS (Sarvam Bulbul)"
        tts_lang = {"hi": "hi-IN", "mr": "mr-IN"}.get(detected_lang, "en-IN")

        # Prepare text for TTS: strip markdown and trim to ≤3 spoken sentences.
        # The LLM occasionally emits chain-of-thought with markdown formatting
        # (e.g. "**Analyze:**\n* ...") which sounds bad when spoken aloud and
        # can push the text past the 500-char TTS limit.
        from agents import _voice_clean
        tts_text = _voice_clean(bot_response)
        # If still very long, keep only the first 3 sentence-ending segments.
        if len(tts_text) > 450:
            parts = re.split(r'(?<=[।.!?])\s+', tts_text)
            truncated, acc = [], 0
            for part in parts:
                if acc + len(part) > 450:
                    break
                truncated.append(part)
                acc += len(part) + 1
            tts_text = " ".join(truncated) if truncated else tts_text[:450]
        logger.info("Step 7 — TTS input | len=%d | text=%r", len(tts_text), tts_text[:80])

        audio_out_bytes = await sarvam_client.synthesize_speech(
            text=tts_text,
            language=tts_lang,
            voice=os.getenv("DEFAULT_TTS_VOICE", "female_1"),
        )
        logger.info("Step 7 — TTS done | audio_bytes=%d", len(audio_out_bytes))

        # Step 8 — Encode audio for JSON transport
        step = "Step 8: encode audio"
        response_audio_b64 = base64.b64encode(audio_out_bytes).decode("utf-8")

        # Step 9 — Log to Supabase
        step = "Step 9: log to Supabase"
        await supabase_client.log_conversation_turn(
            req.call_id, transcription, bot_response, detected_lang
        )
        logger.debug("Step 9 — Conversation logged to Supabase")

        return {
            "transcription": transcription,
            "response": bot_response,
            "audio_base64": response_audio_b64,
            "language": detected_lang,
            "escalate": escalate,
            "issue_type": issue_type,
            "routing": result.get("routing"),
            "priority": result.get("priority"),
            "ticket_id": result.get("ticket_id"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Pipeline failed at [%s]: %s", step, exc, exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": f"[{step}] {str(exc)}",
                "detail": f"Pipeline failed at: {step}",
                "step": step,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


@app.post("/voice/end")
async def end_call(req: EndCallRequest):
    """End an active call session and record duration in Supabase."""
    logger.info(
        "End call | call_id=%s | duration=%ds", req.call_id, req.duration_seconds
    )

    end_result = await orchestrator.end_call(req.call_id, req.duration_seconds)
    return {
        "call_id": req.call_id,
        "status": "completed",
        "turns": end_result.get("turns", 0),
        "duration_seconds": req.duration_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.websocket("/ws/voice/{call_id}")
async def ws_voice(websocket: WebSocket, call_id: str):
    """Real-time WebSocket voice pipeline (Pipecat).

    The browser should:
      1. Open this WebSocket after calling /voice/start to get a call_id.
      2. Stream raw PCM16 audio (16 kHz mono) as binary messages.
      3. Handle JSON text messages:
           {type: 'transcript',  text, language}
           {type: 'response',    text, escalate, issue_type, routing, ticket_id}
           {type: 'audio_chunk', audio_base64, sentence_index, is_last, text}
           {type: 'error',       message}
    """
    await websocket.accept()
    logger.info("WebSocket connected | call_id=%s", call_id)
    try:
        from voice_pipeline import run_voice_ws_pipeline
        await run_voice_ws_pipeline(
            websocket=websocket,
            call_id=call_id,
            sarvam_client=sarvam_client,
            orchestrator=orchestrator,
        )
    except Exception as exc:
        logger.error("WebSocket pipeline error | call_id=%s | %s", call_id, exc, exc_info=True)
    finally:
        logger.info("WebSocket disconnected | call_id=%s", call_id)


@app.post("/n8n/webhook")
async def n8n_callback(request: Request):
    """
    Receives callback from n8n workflow after escalation processing.
    Updates Supabase with ticket details.
    """
    body = await request.json()
    logger.info("n8n callback received | body=%s", body)

    call_id = body.get("call_id", "")
    ticket_id = body.get("ticket_id", "")
    status = body.get("status", "escalated")

    if call_id:
        await supabase_client.update_escalation(call_id, ticket_id, status)

    return {
        "status": "received",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }



# ---- Mock downstream endpoints (replaces standalone mock_server.py) -------
# n8n workflow calls these during escalation flow.

@app.post("/mock/ticket")
async def mock_create_ticket():
    ticket_id = "TKT-" + str(random.randint(10000, 99999))
    return {"ticket_id": ticket_id, "status": "created", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.post("/mock/sms")
async def mock_send_sms():
    return {"status": "sent", "message": "SMS delivered"}

@app.post("/mock/whatsapp")
async def mock_send_whatsapp():
    return {"status": "sent", "message": "WhatsApp delivered"}

@app.post("/mock/cancel-addon")
async def mock_cancel_addon():
    return {"status": "success", "message": "Add-on cancellation submitted", "effective": "within 2 hours"}

@app.post("/mock/schedule-callback")
async def mock_schedule_callback():
    return {"status": "success", "callback_time": "within 2 hours", "agent": "Senior Support"}

@app.post("/mock/send-bill")
async def mock_send_bill():
    return {"status": "success", "message": "Bill sent to WhatsApp"}

@app.post("/mock/recharge")
async def mock_recharge():
    return {"status": "success", "amount": "processed", "new_balance": "updated"}

@app.post("/mock/activate-data-pack")
async def mock_activate_data_pack():
    return {"status": "success", "data_added": "1GB", "valid_for": "24 hours"}

@app.post("/mock/raise-complaint")
async def mock_raise_complaint():
    complaint_id = "CMP-" + str(random.randint(10000, 99999))
    return {"status": "success", "complaint_id": complaint_id, "sla": "48 hours"}


# ---- Serve React SPA (must be last — catches all unmatched routes) --------
_STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"

if _STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(_STATIC_DIR / "assets")), name="static-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        return FileResponse(str(_STATIC_DIR / "index.html"))


# ---- Entrypoint ----------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=log_level.lower(),
    )
