"""
Direct WebSocket voice pipeline — replaces pipecat_bot.py.

Same entry point: run_voice_ws_pipeline(websocket, call_id, sarvam_client, orchestrator)

Uses simple RMS energy thresholding for VAD.  No pipecat, no torch, no silero.
Sends the same JSON message types the frontend expects:
  {type: transcript, text, language}
  {type: response,   text, escalate, issue_type, routing, ticket_id}
  {type: audio_chunk, audio_base64, sentence_index, is_last, text}
  {type: error, message}
"""

import asyncio
import base64
import logging
import os
import re
import struct

logger = logging.getLogger(__name__)

_SPEECH_RMS = 300           # RMS above this counts as speech
_SILENCE_CHUNKS = 15        # consecutive quiet chunks before end-of-utterance
_MIN_SPEECH_CHUNKS = 2      # discard very short blips
_MAX_AUDIO_BYTES = 30 * 16000 * 2   # 30-second hard cap (PCM16 @ 16 kHz)


def _rms(chunk: bytes) -> float:
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", chunk[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


async def run_voice_ws_pipeline(
    websocket,
    call_id: str,
    sarvam_client,
    orchestrator,
) -> None:
    ctx = orchestrator.active_calls.get(call_id)
    lang = ctx.language if ctx else "hi"

    buf: list[bytes] = []
    speech_chunks = 0
    silence_chunks = 0
    in_speech = False

    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.debug("WebSocket idle timeout | call_id=%s", call_id)
                break

            if msg.get("type") == "websocket.disconnect":
                break

            raw = msg.get("bytes")
            if not raw:
                continue

            energy = _rms(raw)

            if energy >= _SPEECH_RMS:
                in_speech = True
                silence_chunks = 0
                speech_chunks += 1
                buf.append(raw)
                if sum(len(c) for c in buf) >= _MAX_AUDIO_BYTES:
                    lang = await _process(websocket, call_id, b"".join(buf), lang, sarvam_client, orchestrator)
                    buf.clear()
                    speech_chunks = 0
                    silence_chunks = 0
                    in_speech = False
            elif in_speech:
                buf.append(raw)
                silence_chunks += 1
                if silence_chunks >= _SILENCE_CHUNKS:
                    if speech_chunks >= _MIN_SPEECH_CHUNKS:
                        lang = await _process(websocket, call_id, b"".join(buf), lang, sarvam_client, orchestrator)
                    buf.clear()
                    speech_chunks = 0
                    silence_chunks = 0
                    in_speech = False

    except Exception as exc:
        logger.error("voice_pipeline error | call_id=%s | %s", call_id, exc, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)[:200]})
        except Exception:
            pass


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[।.!?])\s+", text)
    out: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        buf = f"{buf} {p}".strip() if buf else p
        if len(buf) >= 20:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [text]


async def _process(
    websocket,
    call_id: str,
    audio: bytes,
    lang: str,
    sarvam_client,
    orchestrator,
) -> str:
    try:
        transcription, lang_code = await sarvam_client.transcribe_audio(audio)
        transcription = (transcription or "").strip()
        _bcp = {"hi-IN": "hi", "mr-IN": "mr", "en-IN": "en"}
        lang = _bcp.get(lang_code, (lang_code.split("-")[0] if lang_code else lang))

        if not transcription:
            logger.info("Empty transcript — skipping | call_id=%s", call_id)
            return lang

        logger.info("STT | lang=%s | text=%r | call_id=%s", lang, transcription[:80], call_id)
        await websocket.send_json({"type": "transcript", "text": transcription, "language": lang})

        result = await orchestrator.process_turn(call_id, transcription, lang)
        bot = result["response"]
        await websocket.send_json({
            "type": "response",
            "text": bot,
            "escalate": result["escalated"],
            "issue_type": result.get("issue_type", ""),
            "routing": result.get("routing", ""),
            "ticket_id": result.get("ticket_id", ""),
        })

        from agents import _voice_clean
        tts_text = _voice_clean(bot)
        if len(tts_text) > 450:
            parts = re.split(r"(?<=[।.!?])\s+", tts_text)
            truncated, acc = [], 0
            for p in parts:
                if acc + len(p) > 450:
                    break
                truncated.append(p)
                acc += len(p) + 1
            tts_text = " ".join(truncated) if truncated else tts_text[:450]

        tts_lang = {"hi": "hi-IN", "mr": "mr-IN"}.get(lang, "en-IN")
        sentences = _split_sentences(tts_text)
        for i, sent in enumerate(sentences):
            if not sent.strip():
                continue
            try:
                audio_out = await sarvam_client.synthesize_speech(
                    text=sent,
                    language=tts_lang,
                    voice=os.getenv("DEFAULT_TTS_VOICE", "female_1"),
                )
                await websocket.send_json({
                    "type": "audio_chunk",
                    "audio_base64": base64.b64encode(audio_out).decode(),
                    "sentence_index": i,
                    "is_last": (i == len(sentences) - 1),
                    "text": sent,
                })
            except Exception as tts_err:
                logger.warning("TTS sentence %d failed: %s", i, tts_err)

    except Exception as exc:
        logger.error("_process error | call_id=%s | %s", call_id, exc, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)[:200]})
        except Exception:
            pass

    return lang
