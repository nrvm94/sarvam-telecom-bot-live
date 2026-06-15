"""
Sarvam AI API Client
Handles all interactions with Sarvam AI services:
  - Speech-to-Text  → POST https://api.sarvam.ai/speech-to-text  (multipart/form-data)
  - Chat Completions → POST https://api.sarvam.ai/v1/chat/completions (OpenAI-compat)
  - Text-to-Speech  → POST https://api.sarvam.ai/text-to-speech  (JSON)

Auth:
  - /v1/chat/completions  uses  Authorization: Bearer <key>
  - /speech-to-text and /text-to-speech  use  api-subscription-key: <key>
"""

import base64
import io
import logging
import os
import re
import time
import wave

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chain-of-thought stripper
# ---------------------------------------------------------------------------

# Matches numbered CoT section headers AND analysis-prose lines that the model
# sometimes emits in the "answer area" (e.g. "Wait, let me reconsider...").
# Used by _strip_cot() English backward-walk to skip over them rather than stop.
_ANALYSIS_LINE = re.compile(
    r'^\*+\s+\*\*'                  # sub-bullet headers  **Foo**
    r'|^Wait[,\s]|^Actually[,\s]'   # self-correction prose
    r'|^Let me |^I should |^Hmm[,\s]'
    r'|^The response is|^So the (final|answer)',
    re.IGNORECASE,
)


def _strip_cot(text: str, language: str = "en") -> str:
    """Remove LLM chain-of-thought reasoning from the response.

    sarvam-105b sometimes externalises its step-by-step thinking in the content
    field before the final answer.  Detected by numbered markdown headers like
    "1. **Deconstruct the Request:**".  We extract the final clean customer
    response that appears after all reasoning steps.

    Strategy:
    - Hindi/Marathi: extract the last contiguous block of Devanagari sentences.
    - English: take the last non-bulleted, non-numbered paragraph.
    """
    if not text:
        return text

    # Fast-path: if no CoT markers present, return as-is.
    if not re.search(r'(?m)^\d+\.\s*\*\*', text):
        return text

    logger.warning("LLM CoT detected — extracting clean answer | language=%s", language)

    if language in ("hi", "mr", "hi-IN", "mr-IN"):
        # Devanagari sentences: start with a Devanagari char, end with ।/!/?.
        # Cap at 300 chars to prevent matching across paragraph boundaries.
        sentences = re.findall(
            r'[\u0900-\u097F][^।!?\n]{3,300}[।!?]',
            text,
        )
        if sentences:
            # Take the last ≤3 sentences as the actual response.
            clean = " ".join(s.strip() for s in sentences[-3:])
            if len(clean) > 15:
                return clean
        # Safety net: last 200 chars of Devanagari text
        deva_chars = re.findall(r'[\u0900-\u097F\s.,!?।₹0-9]+', text)
        if deva_chars:
            fallback = deva_chars[-1].strip()[-200:].strip()
            if len(fallback) > 15:
                return fallback

    # English / fallback: walk backward collecting clean lines.
    # Stop at numbered CoT section headers; skip (continue) analysis-prose lines.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    result: list[str] = []
    for line in reversed(lines):
        if re.match(r'^\d+\.\s*\*\*|^#+\s', line):  # major CoT header → stop
            break
        if _ANALYSIS_LINE.match(line):               # analysis prose → skip
            continue
        result.insert(0, line)
    clean = " ".join(result).strip()
    if len(clean) > 15:
        return clean

    return text  # fallback — return original rather than nothing

# Map generic voice names → real Sarvam Bulbul v3 speaker IDs
# bulbul:v3 female voices: ritu, priya, neha, pooja, simran, kavya, ishita, ...
# bulbul:v3 male voices:   rahul, amit, aditya, rohan, ashutosh, dev, varun, ...
VOICE_MAP = {
    "female_1": "ritu",
    "female_2": "priya",
    "female_3": "neha",
    "male_1": "rahul",
    "male_2": "amit",
    "male_3": "aditya",
    # Pass-through any real speaker name as-is
}

# Mobile generation terms: only these need explicit normalization.
# Sarvam TTS handles GB, MB, VoLTE, OTP, UPI etc. natively via enable_preprocessing.
_GENERATION_REPLACEMENTS = [
    (r'\b5[Gg]\b', 'five G'),
    (r'\b4[Gg]\b', 'four G'),
    (r'\b3[Gg]\b', 'three G'),
    (r'\b2[Gg]\b', 'two G'),
]

# ---- Number-to-words helpers -----------------------------------------------

_EN_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
            "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
            "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty",
            "sixty", "seventy", "eighty", "ninety"]


def _num_to_words_en(n: int) -> str:
    if n < 0: return "minus " + _num_to_words_en(-n)
    if n == 0: return "zero"
    if n < 20: return _EN_ONES[n]
    if n < 100: return _EN_TENS[n // 10] + (" " + _EN_ONES[n % 10] if n % 10 else "")
    if n < 1000: return _EN_ONES[n // 100] + " hundred" + (" " + _num_to_words_en(n % 100) if n % 100 else "")
    if n < 100000: return _num_to_words_en(n // 1000) + " thousand" + (" " + _num_to_words_en(n % 1000) if n % 1000 else "")
    return str(n)  # fallback for very large numbers


_HI_ONES = ["", "ek", "do", "teen", "chaar", "paanch", "cheh", "saat", "aath", "nau",
            "das", "gyarah", "barah", "terah", "chaudah", "pandrah", "solah",
            "satrah", "atharah", "unnees"]
_HI_TENS = ["", "", "bees", "tees", "chaalees", "pachaas",
            "saath", "sattar", "assee", "nabbe"]


def _num_to_words_hi(n: int) -> str:
    if n < 0: return "minus " + _num_to_words_hi(-n)
    if n == 0: return "shoonya"
    if n < 20: return _HI_ONES[n]
    if n < 100: return _HI_TENS[n // 10] + (" " + _HI_ONES[n % 10] if n % 10 else "")
    if n < 1000: return _HI_ONES[n // 100] + " sau" + (" " + _num_to_words_hi(n % 100) if n % 100 else "")
    if n < 100000: return _num_to_words_hi(n // 1000) + " hazaar" + (" " + _num_to_words_hi(n % 1000) if n % 1000 else "")
    return str(n)


def normalize_for_tts(text: str, language: str) -> str:
    """
    Minimal pre-processing before sending to Sarvam TTS.

    Sarvam bulbul:v3 with enable_preprocessing=True handles numbers, abbreviations,
    and most symbols natively. We only fix the two things it can't:
      1. Mobile generation terms (5G/4G) — TTS may read as raw digits or wrong words.
      2. Currency symbol ₹ — not always parsed correctly.
    """
    is_hindi = language in ("hi", "hi-IN", "mr", "mr-IN")

    # Mobile generation terms (word-boundary, case-insensitive)
    for pattern, replacement in _GENERATION_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)

    # Currency symbol → spoken word
    text = text.replace("₹", "रुपये " if is_hindi else "rupees ")

    return text


_TTS_MAX_CHARS = 490  # Sarvam TTS limit is 500; use 490 for safety


def _split_text_for_tts(text: str, max_chars: int = _TTS_MAX_CHARS) -> list:
    """
    Split text into chunks of at most max_chars, preferring sentence boundaries
    (। . ! ?) then word boundaries. Works for Hindi, Marathi, and English.
    """
    if len(text) <= max_chars:
        return [text]

    sentence_splitter = re.compile(r'(?<=[।.!?])\s+')
    sentences = sentence_splitter.split(text)

    chunks = []
    current = ""
    for sentence in sentences:
        # A single sentence longer than max_chars — split at word boundaries
        if len(sentence) > max_chars:
            words = sentence.split()
            for word in words:
                candidate = (current + " " + word).strip() if current else word
                if len(candidate) > max_chars:
                    if current:
                        chunks.append(current.strip())
                    current = word
                else:
                    current = candidate
        else:
            candidate = (current + " " + sentence).strip() if current else sentence
            if len(candidate) > max_chars:
                if current:
                    chunks.append(current.strip())
                current = sentence
            else:
                current = candidate

    if current:
        chunks.append(current.strip())

    # Safety: hard-truncate any chunk that somehow exceeds the limit
    # (e.g. a single token with no whitespace > max_chars)
    safe = []
    for chunk in chunks:
        if chunk:
            while len(chunk) > max_chars:
                safe.append(chunk[:max_chars])
                chunk = chunk[max_chars:]
            if chunk:
                safe.append(chunk)
    return safe


def _concat_wav_buffers(buffers: list) -> bytes:
    """Concatenate a list of WAV byte-buffers into a single WAV."""
    if len(buffers) == 1:
        return buffers[0]

    all_frames = []
    params = None
    for buf in buffers:
        with wave.open(io.BytesIO(buf)) as wf:
            if params is None:
                params = wf.getparams()
            all_frames.append(wf.readframes(wf.getnframes()))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setparams(params)
        for frames in all_frames:
            wf.writeframes(frames)
    return out.getvalue()


class SarvamClient:
    """
    Async client for Sarvam AI APIs (STT, LLM, TTS).
    Uses aiohttp for non-blocking HTTP requests.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.sarvam.ai"):
        self.api_key = api_key

        # Strip /v1 suffix — we build paths explicitly per endpoint
        clean = base_url.rstrip("/")
        if clean.endswith("/v1"):
            clean = clean[:-3]
        self.base_url = clean  # e.g. "https://api.sarvam.ai"

        logger.info(
            "SarvamClient initialised | base_url=%s | key=%s***",
            self.base_url,
            api_key[:8] if api_key else "MISSING",
        )

    # ------------------------------------------------------------------
    # Auth header helpers
    # ------------------------------------------------------------------

    def _bearer_headers(self) -> dict:
        """For OpenAI-compatible /v1/ endpoints."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _subscription_headers(self) -> dict:
        """For Sarvam-native endpoints (STT, TTS)."""
        return {
            "api-subscription-key": self.api_key,
        }

    # ------------------------------------------------------------------
    # Method 1 — Speech-to-Text (Saaras)
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self, audio_bytes: bytes, language: str = None
    ) -> tuple[str, str]:
        """
        Transcribe raw audio bytes to text using Sarvam Saaras v3 STT.

        The API expects multipart/form-data — NOT base64 JSON.
        Saaras v3 auto-detects the spoken language when language_code="unknown".

        Args:
            audio_bytes: Raw WebM/Opus or WAV audio.
            language:    BCP-47 hint (e.g. "hi-IN"). Pass None for auto-detection.

        Returns:
            Tuple of (transcript, detected_language_code).
            detected_language_code is a BCP-47 code e.g. "hi-IN", "en-IN", "mr-IN".
        """
        lang_code = language if language else "unknown"
        logger.debug(
            "STT | language_hint=%s | audio_bytes=%d", lang_code, len(audio_bytes)
        )

        # Auto-detect format: WAV starts with b'RIFF'; everything else is WebM.
        if audio_bytes[:4] == b'RIFF':
            _fname, _ctype = "audio.wav", "audio/wav"
        else:
            _fname, _ctype = "audio.webm", "audio/webm"

        data = aiohttp.FormData()
        data.add_field("file", audio_bytes, filename=_fname, content_type=_ctype)
        data.add_field("model", "saaras:v3")
        data.add_field("mode", "transcribe")
        data.add_field("language_code", lang_code)

        url = f"{self.base_url}/speech-to-text"
        logger.debug("STT | POST %s | lang=%s | bytes=%d", url, lang_code, len(audio_bytes))

        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=data,
                headers=self._subscription_headers(),
            ) as resp:
                elapsed_ms = (time.time() - t0) * 1000
                body = await resp.json(content_type=None)
                logger.debug("STT | status=%d | elapsed=%.0fms | body=%s", resp.status, elapsed_ms, str(body)[:300])

                if resp.status != 200:
                    logger.error(
                        "STT failed | status=%d | body=%s",
                        resp.status, str(body)[:500],
                    )
                    raise RuntimeError(
                        f"Sarvam STT failed: HTTP {resp.status} | {body}"
                    )

                transcript = (
                    body.get("transcript")
                    or body.get("text")
                    or body.get("transcription")
                    or ""
                )
                detected_lang = body.get("language_code") or lang_code
                logger.info(
                    "STT done | detected=%s | elapsed=%.0fms | transcript=%r",
                    detected_lang, elapsed_ms, transcript[:120],
                )
                return transcript, detected_lang

    # ------------------------------------------------------------------
    # Method 2 — Chat Completions (LLM)
    # ------------------------------------------------------------------

    async def generate_response(
        self, query: str, context: str, language: str = "hi", history: list = None,
        system_prompt_override: str = None
    ) -> str:
        """
        Generate an Airtel customer-support reply using Sarvam LLM.

        Uses /v1/chat/completions (OpenAI-compatible) with Authorization: Bearer.

        Args:
            query:                   Customer's transcription / question.
            context:                 RAG context (appended to user message if non-empty).
            language:                "hi" for Hindi, "en" for English.
            history:                 Prior turns as [{"role": "user"|"assistant", "content": "..."}].
            system_prompt_override:  When provided, replaces the default generic system prompt.
                                     Use this to inject customer-specific instructions and data.

        Returns:
            Bot response text.
        """
        system_content = system_prompt_override or (
            "STRICT: Output ONLY the final customer response — no reasoning steps, no numbered analysis, "
            "no chain-of-thought. Start directly with your answer to the customer. "
            "You are a helpful Airtel customer support agent. "
            "Detect the language of the user's message and respond in the SAME language and style. "
            "If the user writes in Hindi (Devanagari script), respond only in Hindi using Devanagari script — no Roman Hindi. "
            "If the user writes in English, respond only in English. "
            "If the user mixes Hindi and English (code-switching), mirror their style naturally. "
            "Keep your response concise, 2-3 sentences maximum."
        )

        user_content = query
        if context and context.strip():
            user_content += f"\n\nRelevant Information:\n{context}"

        # Build messages: system → history (last 10 msgs = 5 exchanges) → current query
        messages = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history[-10:])
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": "sarvam-105b",
            "messages": messages,
            "temperature": 0.3,
            # Do NOT set max_tokens — sarvam-105b is a reasoning model
            # that spend ~1500-2000 tokens on internal chain-of-thought before writing
            # the actual response. Any hard cap will truncate during reasoning, leaving
            # content=None. Let the model finish naturally (finish_reason='stop').
        }

        url = f"{self.base_url}/v1/chat/completions"
        logger.debug(
            "LLM | POST %s | query=%r | context_len=%d | history_msgs=%d",
            url, query[:80], len(context), len(messages),
        )

        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=self._bearer_headers(),  # Authorization: Bearer
            ) as resp:
                elapsed_ms = (time.time() - t0) * 1000
                body = await resp.json(content_type=None)
                logger.debug(
                    "LLM | status=%d | elapsed=%.0fms | body=%s",
                    resp.status, elapsed_ms, str(body)[:400],
                )

                if resp.status != 200:
                    logger.error(
                        "LLM failed | status=%d | elapsed=%.0fms | body=%s",
                        resp.status, elapsed_ms, str(body)[:500],
                    )
                    raise RuntimeError(
                        f"Sarvam LLM failed: HTTP {resp.status} | {body}"
                    )

                choices = body.get("choices") or [{}]
                choice0 = choices[0]
                message = choice0.get("message") or {}
                finish_reason = choice0.get("finish_reason", "unknown")

                # sarvam-105b may return content=None with reasoning_content populated
                # when token budget runs out. Fall back to reasoning_content in that case.
                response_text = (
                    message.get("content")
                    or message.get("reasoning_content")
                    or ""
                )

                if not response_text:
                    logger.warning(
                        "LLM returned empty content | finish_reason=%s | elapsed=%.0fms | body=%s",
                        finish_reason, elapsed_ms, str(body)[:300],
                    )

                # Strip chain-of-thought reasoning if the model externalised it.
                response_text = _strip_cot(response_text, language)

                logger.info(
                    "LLM done | elapsed=%.0fms | finish_reason=%s | response_len=%d | response=%r",
                    elapsed_ms, finish_reason, len(response_text), str(response_text)[:120],
                )
                return response_text

    # ------------------------------------------------------------------
    # Method 2b — Chat Completions WITH tool-calling
    # ------------------------------------------------------------------

    async def chat_with_tools(
        self, messages: list, tools: list, temperature: float = 0.3
    ) -> dict:
        """
        Low-level chat call that supports OpenAI-style function/tool calling.

        Returns the raw assistant `message` dict, which may contain either
        `content` (final answer) or `tool_calls` (the model wants data fetched).
        The caller runs the tool, appends a {"role":"tool",...} message, and
        calls this again. NOTE: the Sarvam endpoint requires `tools` to be sent
        on EVERY call once any tool message is in the history.

        Args:
            messages:    Full OpenAI-format message list (system, user, assistant, tool).
            tools:       OpenAI tools schema array.
            temperature: Sampling temperature.

        Returns:
            The assistant message dict from choices[0].message.
        """
        payload = {
            "model": "sarvam-105b",
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
        }

        url = f"{self.base_url}/v1/chat/completions"
        logger.debug("LLM+tools | POST %s | msgs=%d | tools=%d", url, len(messages), len(tools))

        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=self._bearer_headers()
            ) as resp:
                elapsed_ms = (time.time() - t0) * 1000
                body = await resp.json(content_type=None)
                logger.debug("LLM+tools | status=%d | elapsed=%.0fms | body=%s", resp.status, elapsed_ms, str(body)[:400])

                if resp.status != 200:
                    logger.error("LLM+tools failed | status=%d | elapsed=%.0fms | body=%s", resp.status, elapsed_ms, str(body)[:500])
                    raise RuntimeError(
                        f"Sarvam LLM (tools) failed: HTTP {resp.status} | {body}"
                    )

                choices = body.get("choices") or [{}]
                message = choices[0].get("message") or {}
                logger.info(
                    "LLM+tools done | elapsed=%.0fms | tool_calls=%s | content_len=%d",
                    elapsed_ms, bool(message.get("tool_calls")),
                    len(message.get("content") or ""),
                )
                return message

    # ------------------------------------------------------------------
    # Method 3 — Text-to-Speech (Bulbul)
    # ------------------------------------------------------------------

    async def synthesize_speech(
        self,
        text: str,
        language: str = "hi-IN",
        voice: str = "female_1",
    ) -> bytes:
        """
        Convert text to speech using Sarvam Bulbul TTS.

        Automatically splits text that exceeds the 500-character API limit into
        sentence-boundary chunks and concatenates the resulting WAV buffers.

        Args:
            text:     Text to synthesise.
            language: BCP-47 code, e.g. "hi-IN" or "mr-IN".
            voice:    Generic voice alias ("female_1") or real speaker name ("ritu").

        Returns:
            Raw WAV audio bytes.
        """
        text = normalize_for_tts(text, language)
        # Map generic alias → real speaker name; pass-through if already real
        speaker = VOICE_MAP.get(voice, voice)
        # Ensure language has region suffix
        lang_code = language if "-" in language else {
            "hi": "hi-IN", "mr": "mr-IN", "en": "en-IN"
        }.get(language, f"{language}-IN")

        chunks = _split_text_for_tts(text)
        if len(chunks) > 1:
            logger.info(
                "TTS | text too long (%d chars) — splitting into %d chunks | language=%s | speaker=%s",
                len(text), len(chunks), lang_code, speaker,
            )

        t_total = time.time()
        audio_buffers = []
        for idx, chunk in enumerate(chunks, start=1):
            audio = await self._synthesize_chunk(chunk, lang_code, speaker, idx, len(chunks))
            audio_buffers.append(audio)

        combined = _concat_wav_buffers(audio_buffers)
        total_ms = (time.time() - t_total) * 1000
        logger.info(
            "TTS done | chunks=%d | total_elapsed=%.0fms | language=%s | speaker=%s | text_len=%d | output_bytes=%d",
            len(chunks), total_ms, lang_code, speaker, len(text), len(combined),
        )
        return combined

    async def _synthesize_chunk(
        self, text: str, lang_code: str, speaker: str, chunk_idx: int = 1, total_chunks: int = 1
    ) -> bytes:
        """Send a single ≤500-char chunk to the Sarvam TTS API and return WAV bytes."""
        payload = {
            "inputs": [text],
            "target_language_code": lang_code,
            "speaker": speaker,
            "model": "bulbul:v3",
            "pace": 1.0,
            "speech_sample_rate": 8000,
            "enable_preprocessing": True,
        }

        url = f"{self.base_url}/text-to-speech"
        logger.debug(
            "TTS chunk %d/%d | POST %s | text_len=%d | language=%s | speaker=%s | text=%r",
            chunk_idx, total_chunks, url, len(text), lang_code, speaker, text[:60],
        )

        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={
                    **self._subscription_headers(),  # api-subscription-key
                    "Content-Type": "application/json",
                },
            ) as resp:
                elapsed_ms = (time.time() - t0) * 1000
                body = await resp.json(content_type=None)
                logger.debug(
                    "TTS chunk %d/%d | status=%d | elapsed=%.0fms | body_keys=%s",
                    chunk_idx, total_chunks, resp.status, elapsed_ms,
                    list(body.keys()) if isinstance(body, dict) else "non-dict",
                )

                if resp.status != 200:
                    logger.error(
                        "TTS chunk %d/%d failed | status=%d | elapsed=%.0fms | language=%s | speaker=%s | body=%s",
                        chunk_idx, total_chunks, resp.status, elapsed_ms, lang_code, speaker, str(body)[:500],
                    )
                    raise RuntimeError(
                        f"Sarvam TTS failed: HTTP {resp.status} | {body}"
                    )

                # Response: {"audios": ["<base64_wav>"]}
                audios = body.get("audios") or []
                if not audios:
                    logger.error(
                        "TTS chunk %d/%d returned empty audios | elapsed=%.0fms | body=%s",
                        chunk_idx, total_chunks, elapsed_ms, str(body)[:300],
                    )
                    raise RuntimeError(
                        f"Sarvam TTS returned empty audios list | body={body}"
                    )

                audio_bytes = base64.b64decode(audios[0])
                logger.info(
                    "TTS chunk %d/%d done | elapsed=%.0fms | text_len=%d | output_bytes=%d",
                    chunk_idx, total_chunks, elapsed_ms, len(text), len(audio_bytes),
                )
                return audio_bytes
