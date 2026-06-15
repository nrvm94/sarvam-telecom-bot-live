"""
Pipecat-based real-time voice pipeline for Sarvam Telecom Bot.

Architecture (per WebSocket session)
-------------------------------------
  Browser PCM16 (WebSocket binary)
  → FastAPIWebsocketTransport.input()   [deserialises bytes → InputAudioRawFrame]
  → VADProcessor (SileroVAD)            [emits VADUserStartedSpeakingFrame / Stopped]
  → SarvamDualSTTService                [buffers speech, runs parallel hi/en STT,
                                          detects language, yields TranscriptionFrame]
  → OrchestratorTTSProcessor            [RAG + LLM + sentence-level TTS,
                                          sends JSON over WebSocket directly]
  → FastAPIWebsocketTransport.output()  [passthrough; we don't push audio frames here]

Latency improvements over HTTP pipeline
----------------------------------------
  • SileroVAD stop_secs=0.6 s  vs 1.5 s RMS silence in HTTP mode
  • Sentence-level TTS streaming: first sentence plays while LLM is generating
    the rest (in practice, the LLM responds as a full string; we split after)
  • No MediaRecorder buffering: PCM chunks stream continuously from the browser
"""

import base64
import os
import re
from datetime import datetime, timezone
from typing import AsyncGenerator

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    Language,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)


import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serializer: raw PCM16 bytes → InputAudioRawFrame
# ---------------------------------------------------------------------------

class PcmAudioSerializer(FrameSerializer):
    """Deserialises raw PCM16 binary WebSocket messages into InputAudioRawFrame.

    The browser sends raw 16-bit little-endian PCM at 16 kHz (mono).
    Output serialisation returns None — audio is sent back as JSON via
    OrchestratorTTSProcessor, not through the Pipecat output transport.
    """

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes) and data:
            return InputAudioRawFrame(
                audio=data,
                sample_rate=16000,
                num_channels=1,
            )
        return None  # Ignore text/control messages

    async def serialize(self, frame: Frame) -> str | bytes | None:
        return None  # Audio sent directly via WebSocket in OrchestratorTTSProcessor


# ---------------------------------------------------------------------------
# Dual STT Service (SegmentedSTTService subclass)
# ---------------------------------------------------------------------------

class SarvamDualSTTService(SegmentedSTTService):
    """Sarvam batch-HTTP STT with parallel dual-language detection.

    Inherits from SegmentedSTTService so Pipecat handles VAD-driven audio
    buffering.  Only run_stt() is overridden to use the same dual-batch
    parallel approach as the HTTP pipeline in main.py.
    """

    def __init__(self, sarvam_client, call_context: dict, **kwargs):
        """
        Args:
            sarvam_client: SarvamClient instance for HTTP STT calls.
            call_context: Shared mutable dict with 'language' and 'call_id' keys.
                          'language' is read at the start of each turn and updated
                          to the detected language after STT.
        """
        super().__init__(sample_rate=16000, **kwargs)
        self._sarvam = sarvam_client
        self._ctx = call_context

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Run Saaras v3 STT with auto language detection; yield TranscriptionFrame."""
        try:
            transcription, detected_lang_code = await self._sarvam.transcribe_audio(audio)
            transcription = (transcription or '').strip()
            _bcp_to_short = {'hi-IN': 'hi', 'mr-IN': 'mr', 'en-IN': 'en'}
            detected_lang = _bcp_to_short.get(
                detected_lang_code,
                detected_lang_code.split('-')[0] if detected_lang_code else 'hi'
            )
            if not transcription:
                logger.info('STT: empty transcript — skipping')
                return
            logger.info('STT done | detected=%s | text=%r', detected_lang, transcription[:80])
            self._ctx['language'] = detected_lang
            _lang_map = {'hi': Language.HI_IN, 'mr': Language.MR_IN, 'en': Language.EN_IN}
            yield TranscriptionFrame(
                text=transcription,
                user_id='',
                timestamp=datetime.now(timezone.utc).isoformat(),
                language=_lang_map.get(detected_lang, Language.EN_IN),
            )
        except Exception as exc:
            logger.error('SarvamDualSTTService.run_stt error: %s', exc, exc_info=True)


# ---------------------------------------------------------------------------
# Orchestrator + Sentence TTS Processor
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split text at sentence boundaries for progressive TTS streaming.

    Groups short fragments together so each TTS call is at least 20 chars.
    """
    # Split on sentence-ending punctuation (। for Hindi, . ! ? for English)
    parts = re.split(r'(?<=[।.!?])\s+', text)
    sentences: list[str] = []
    buf = ''
    for part in parts:
        part = part.strip()
        if not part:
            continue
        buf = (buf + ' ' + part).strip() if buf else part
        if len(buf) >= 20:
            sentences.append(buf)
            buf = ''
    if buf:
        sentences.append(buf)
    return sentences if sentences else [text]


class OrchestratorTTSProcessor(FrameProcessor):
    """Receives TranscriptionFrames; runs RAG+LLM+TTS; sends JSON to browser.

    Non-TranscriptionFrame frames are passed through unchanged so system
    frames (StartFrame, EndFrame, etc.) propagate correctly.
    """

    def __init__(
        self,
        orchestrator,
        sarvam_client,
        websocket,
        call_context: dict,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._orchestrator = orchestrator
        self._sarvam = sarvam_client
        self._ws = websocket
        self._ctx = call_context

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            await self._handle_transcription(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_transcription(self, frame: TranscriptionFrame):
        transcription = frame.text
        detected_lang = self._ctx.get('language', 'hi')
        call_id = self._ctx.get('call_id', '')

        try:
            # Notify browser of transcript immediately (before LLM)
            await self._ws.send_json({
                'type': 'transcript',
                'text': transcription,
                'language': detected_lang,
            })

            # ---- Run orchestrator (RAG + LLM + escalation) ----
            result = await self._orchestrator.process_turn(
                call_id, transcription, detected_lang
            )
            bot_response = result['response']
            escalated = result['escalated']

            logger.info(
                "Orchestrator done | escalated=%s | response=%r",
                escalated, bot_response[:80],
            )

            # Send response metadata
            await self._ws.send_json({
                'type': 'response',
                'text': bot_response,
                'escalate': escalated,
                'issue_type': result.get('issue_type', ''),
                'routing': result.get('routing', ''),
                'ticket_id': result.get('ticket_id', ''),
            })

            # ---- TTS: split into sentences and stream ----
            from agents import _voice_clean
            tts_text = _voice_clean(bot_response)

            # Trim very long responses
            if len(tts_text) > 450:
                parts = re.split(r'(?<=[।.!?])\s+', tts_text)
                truncated, acc = [], 0
                for part in parts:
                    if acc + len(part) > 450:
                        break
                    truncated.append(part)
                    acc += len(part) + 1
                tts_text = ' '.join(truncated) if truncated else tts_text[:450]

            tts_lang = {'hi': 'hi-IN', 'mr': 'mr-IN'}.get(detected_lang, 'en-IN')
            sentences = _split_sentences(tts_text)

            for i, sentence in enumerate(sentences):
                if not sentence.strip():
                    continue
                try:
                    audio_bytes = await self._sarvam.synthesize_speech(
                        text=sentence,
                        language=tts_lang,
                        voice=os.getenv('DEFAULT_TTS_VOICE', 'female_1'),
                    )
                    await self._ws.send_json({
                        'type': 'audio_chunk',
                        'audio_base64': base64.b64encode(audio_bytes).decode(),
                        'sentence_index': i,
                        'is_last': (i == len(sentences) - 1),
                        'text': sentence,
                    })
                    logger.debug("TTS chunk sent | idx=%d | bytes=%d", i, len(audio_bytes))
                except Exception as tts_err:
                    logger.warning("TTS sentence %d error: %s", i, tts_err)

        except Exception as exc:
            logger.error("OrchestratorTTSProcessor error: %s", exc, exc_info=True)
            try:
                await self._ws.send_json({
                    'type': 'error',
                    'message': str(exc)[:200],
                })
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

async def run_voice_ws_pipeline(
    websocket,
    call_id: str,
    sarvam_client,
    orchestrator,
) -> None:
    """Create and run the Pipecat pipeline for one WebSocket voice session.

    Blocks until the WebSocket disconnects (or the pipeline ends).

    Args:
        websocket: FastAPI WebSocket (already accepted).
        call_id: Active call identifier from /voice/start.
        sarvam_client: SarvamClient instance.
        orchestrator: VoiceOrchestrator instance.
    """
    # Shared mutable state for STT ↔ Orchestrator language handoff
    _ctx = orchestrator.active_calls.get(call_id)
    call_context: dict = {
        'call_id': call_id,
        'language': _ctx.language if _ctx else 'hi',  # Updated after each turn by SarvamDualSTTService
    }

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=False,   # Audio returned as JSON base64, not raw bytes
            serializer=PcmAudioSerializer(),
        ),
    )

    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(
                confidence=0.65,   # Slightly lower for telephony / browser mic
                start_secs=0.2,    # 200 ms onset confirmation
                stop_secs=0.6,     # 600 ms silence → end of turn (vs 1500 ms in HTTP)
                min_volume=0.4,
            ),
        ),
    )

    stt = SarvamDualSTTService(
        sarvam_client=sarvam_client,
        call_context=call_context,
    )

    orch = OrchestratorTTSProcessor(
        orchestrator=orchestrator,
        sarvam_client=sarvam_client,
        websocket=websocket,
        call_context=call_context,
    )

    pipeline = Pipeline([
        transport.input(),
        vad,
        stt,
        orch,
        transport.output(),
    ])

    worker = PipelineWorker(pipeline)
    runner = PipelineRunner()
    await runner.add_workers(worker)
    await runner.run()
