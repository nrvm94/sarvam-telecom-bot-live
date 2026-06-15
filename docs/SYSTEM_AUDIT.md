# System Audit — Sarvam Telecom Bot
**Date:** 2026-06-14
**Auditor:** Code-trace analysis of commit `6e5a358` + session fixes
**Status:** Updated 2026-06-14 — all identified issues resolved. See `docs/IMPROVEMENTS.md` for full change log.

---

## 1. Project Overview

This is an AI-powered voice customer support bot for Airtel, built as a portfolio/demo project using Sarvam AI's Indian-language APIs. A user opens a web interface, enters an Airtel phone number, clicks "Start Call", and can speak naturally in Hindi, English, or Marathi. The bot transcribes the speech (STT), looks up the caller's account data, retrieves relevant knowledge-base content (RAG), generates a spoken response using a large language model (LLM), converts it back to audio (TTS), and plays it back. Complex issues trigger an escalation workflow via n8n that notifies a support agent on WhatsApp. The bot maintains full multi-turn conversation state and supports mid-call language switching.

### Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + Vite (port 3000), Tailwind CSS |
| Backend | Python 3, FastAPI + Uvicorn (port 8000) |
| Real-time pipeline | Pipecat-AI (WebSocket, SileroVAD, custom STT/TTS processors) |
| STT | Sarvam saaras:v3 (`/speech-to-text`, auto language detection) |
| LLM | Sarvam sarvam-105b (`/v1/chat/completions`, OpenAI-compatible) |
| TTS | Sarvam bulbul:v3 (`/text-to-speech`) |
| Vector DB | ChromaDB 0.4.18 (local, persistent) |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` (local) |
| Database | Supabase (PostgreSQL) — optional, has mock fallback |
| Escalation | n8n (Docker) → 360dialog sandbox (WhatsApp) |
| Mock downstream | Custom FastAPI server on port 5000 |
| Auth/Config | python-dotenv, `.env` file |

### Entry Points

```
# Required — all 4 must run simultaneously for full escalation flow
docker start n8n                                    # n8n on port 5678
cd backend && python -m uvicorn main:app --reload   # FastAPI on port 8000
cd backend && python mock_server.py                 # Mock services on port 5000
cd frontend && npm run dev                          # Vite on port 3000
```

The user navigates to `http://localhost:3000`. All `/voice/*`, `/health`, `/n8n`, and `/ws/*` requests are proxied by Vite to `http://localhost:8000`.

---

## 2. Architecture Map

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser (localhost:3000)                                           │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  VoiceBot.jsx                                               │   │
│  │  ┌─────────────┐   ┌──────────────────────────────────┐    │   │
│  │  │ Web Audio   │   │ AudioQueue (progressive playback) │    │   │
│  │  │ API (PCM16) │   │ (Web Audio API, WAV decode)       │    │   │
│  │  │ ScriptProc  │   └──────────────────────────────────┘    │   │
│  │  └──────┬──────┘                         ▲                 │   │
│  └─────────┼─────────────────────────────────┼─────────────────┘   │
│            │ PCM16 binary frames             │ JSON {type, audio}  │
│            ▼                                 │                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  WebSocket /ws/voice/{call_id}          Vite proxy (ws:)    │   │
│  └───────────────────────────┬─────────────────────────────────┘   │
│                              │  + HTTP /voice/start                │
│                              │  + HTTP /voice/end                  │
└──────────────────────────────┼─────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI Backend (localhost:8000)                                │
│                                                                  │
│  /voice/start ──► VoiceOrchestrator.start_call()                │
│                   ├─ CustomerProfileAgent (load from JSON DB)   │
│                   └─ SarvamClient.synthesize_speech() [TTS]     │
│                                                                  │
│  /ws/voice/{id} ──► Pipecat Pipeline:                           │
│   ┌──────────────────────────────────────────────┐              │
│   │  FastAPIWebsocketTransport.input()           │              │
│   │    ↓ InputAudioRawFrame (PCM16)              │              │
│   │  VADProcessor (SileroVAD)                    │              │
│   │    ↓ VADUserStartedSpeakingFrame             │              │
│   │    ↓ VADUserStoppedSpeakingFrame             │              │
│   │  SarvamDualSTTService                        │              │
│   │    ├─ hi-IN STT ─┐ parallel asyncio.gather  │              │
│   │    ├─ en-IN STT ─┤ (3-way when pref=hi)     │              │
│   │    └─ mr-IN STT ─┘                           │              │
│   │    ↓ TranscriptionFrame (text + lang)        │              │
│   │  OrchestratorTTSProcessor                    │              │
│   │    ├─ VoiceOrchestrator.process_turn()       │              │
│   │    │   ├─ _is_farewell() check               │              │
│   │    │   ├─ ConversationManager.classify()     │              │
│   │    │   ├─ EscalationAgent.should_escalate()  │              │
│   │    │   ├─ ActionAgent.detect_action_intent() │              │
│   │    │   └─ QueryResolverAgent.run()           │              │
│   │    │       ├─ AirtelKnowledgeBase.query()    │              │
│   │    │       └─ SarvamClient.generate_response() [LLM]       │
│   │    └─ SarvamClient.synthesize_speech() [TTS] │              │
│   │    ↓ JSON {type, audio_chunk, text, ...}     │              │
│   │  FastAPIWebsocketTransport.output()          │              │
│   └──────────────────────────────────────────────┘              │
│                                                                  │
│  /voice/end ──► VoiceOrchestrator.end_call() + Supabase         │
│                                                                  │
│  [Escalation path only]                                          │
│  EscalationAgent ──► trigger_escalation_webhook()               │
└──────────────────────────────────────────────────────────────────┘
         │ STT/LLM/TTS    │ Vector query     │ Call logging   │ n8n webhook
         ▼                ▼                  ▼                ▼
   ┌──────────┐    ┌───────────┐    ┌──────────────┐  ┌──────────┐
   │ Sarvam   │    │ ChromaDB  │    │  Supabase    │  │   n8n    │
   │  AI API  │    │ (local)   │    │ (PostgreSQL) │  │ (Docker) │
   └──────────┘    └───────────┘    └──────────────┘  └─────┬────┘
                                                             │
                                              ┌──────────────┴────────┐
                                              │  Mock Server :5000    │
                                              │  /mock/ticket         │
                                              └──────────────┬────────┘
                                                             │
                                                    ┌────────▼──────────┐
                                                    │  360dialog sandbox│
                                                    │  (WhatsApp API)   │
                                                    └───────────────────┘
```

### External APIs

| API | What It Does Here | Auth Method |
|---|---|---|
| Sarvam `/speech-to-text` | STT: converts PCM audio → text (Hindi/English/Marathi) | `api-subscription-key` header |
| Sarvam `/v1/chat/completions` | LLM: generates customer support responses (sarvam-105b) | `Authorization: Bearer` |
| Sarvam `/text-to-speech` | TTS: converts text → WAV audio (bulbul:v3, speaker=ritu by default) | `api-subscription-key` header |
| Supabase REST | Logs call records, conversation turns, escalation status | `apikey` header |
| n8n webhook | Receives escalation POSTs, orchestrates ticket+WhatsApp | None (unauthenticated) |
| 360dialog sandbox | Sends WhatsApp notification to agent on escalation | `D360-API-KEY` header |

### File Map

| File | What It Does |
|---|---|
| `backend/main.py` | FastAPI app: routes, startup, HTTP voice pipeline, WebSocket handoff |
| `backend/pipecat_bot.py` | Pipecat real-time WebSocket pipeline (VAD→STT→Orchestrator→TTS) |
| `backend/orchestrator.py` | Per-call state, routes to agents, escalation webhook trigger |
| `backend/agents.py` | All agent logic: CustomerProfile, QueryResolver, Action, Escalation; farewell detection |
| `backend/sarvam_client.py` | Sarvam API client: STT, LLM, TTS; CoT stripping; TTS text splitting |
| `backend/language_detect.py` | Vocabulary-based language detection (hi/mr/en) |
| `backend/conversation.py` | Issue classification keywords + `detect_escalation()` (partially dead) |
| `backend/rag_engine.py` | ChromaDB vector store + sentence-transformer embedding; `query()` method |
| `backend/supabase_client.py` | Supabase wrapper with mock fallback when credentials absent |
| `backend/mock_server.py` | Fake downstream services (ticket, WhatsApp, actions) on port 5000 |
| `backend/requirements.txt` | Python dependencies |
| `backend/test_e2e.py` | Manual E2E test script (not a pytest suite) |
| `db/airtel_kb.json` | 22 knowledge base documents (English only) |
| `db/mock_customers.json` | 5 demo customer profiles + `_unknown` fallback |
| `frontend/src/VoiceBot.jsx` | Main React component: PCM capture, WebSocket, audio playback, UI |
| `frontend/src/App.jsx` | Root React component (thin wrapper) |
| `frontend/src/main.jsx` | React entry point |
| `frontend/vite.config.js` | Vite config + dev-server proxy rules |
| `frontend/package.json` | Node dependencies |
| `frontend/index.html` | HTML shell |
| `n8n/workflow_instructions.md` | Step-by-step n8n workflow setup guide |
| `supabase/schema.sql` | SQL to create the `calls` table |
| `docs/API_SPEC.md` | API endpoint documentation |
| `docs/ARCHITECTURE.md` | Architecture overview (may be outdated vs actual code) |
| `docs/BUSINESS_WRITE_UP.md` | Product write-up for demo/portfolio purposes |
| `CLAUDE.md` | Project notes for Claude Code (constraints, run instructions) |
| `.env.example` | Template for required environment variables |
| `docker-compose.yml` | n8n Docker setup |
| `backend/nul` | **ARTIFACT**: Windows garbage file created by accidental `> /dev/null` redirect |
| `backend/chromadb/` | Persisted ChromaDB vector data (auto-populated on first run) |

---

## 3. Feature Inventory

| Feature | What It Should Do | Current Status | Where in Code | Known Issues |
|---|---|---|---|---|
| **Voice Input** | Capture browser mic audio, stream PCM16 to backend | **WORKING** | `VoiceBot.jsx:276-311` (ScriptProcessorNode) | ScriptProcessorNode is deprecated; no AudioWorklet fallback. Echo cancellation is requested but not guaranteed. |
| **STT** | Convert audio to text using saaras:v3 (auto language detection) | **WORKING** | `sarvam_client.py:293-364` | Audio format auto-detected (WAV/WebM). Empty transcript handled. |
| **Language Detection** | Auto-detect Hindi/English/Marathi per turn | **WORKING** | `language_detect.py:146-203` | 33% density threshold (recently lowered). Short-query edge cases exist. |
| **LLM Response** | Generate contextual reply using sarvam-105b | **WORKING** | `sarvam_client.py:370-476` | CoT leak mitigated by 4-layer defense. Response time 3-8s typical. |
| **TTS** | Speak response using bulbul:v3 | **WORKING** | `sarvam_client.py:541-653` | 8kHz sample rate (telephony). Text split at 490 chars. `mr-IN` confirmed correct. |
| **RAG** | Retrieve relevant KB docs for LLM context | **PARTIAL** | `rag_engine.py:118-170`, `agents.py` QueryResolverAgent | No similarity threshold — always returns top-3 even for irrelevant queries. KB is English-only (no Hindi/Marathi documents). |
| **Customer Profile** | Load account data by phone; personalise responses | **WORKING** | `agents.py` CustomerProfileAgent, `db/mock_customers.json` | Only 5 hardcoded demo numbers. Unknown phone falls back to demo account `9876543210`. |
| **Escalation** | Detect complex issue, trigger n8n, send WhatsApp | **PARTIAL** | `agents.py` EscalationAgent, `orchestrator.py:158-186` | Requires Docker + n8n running + 360dialog sandbox opt-in. WhatsApp delivery not guaranteed (sandbox restrictions). |
| **Language Switching** | Switch language mid-call based on spoken input | **WORKING** | `pipecat_bot.py:105-222` (dual/triple STT) | Works via per-turn detection. Initial call always starts as 'hi' (VoiceBot.jsx:337 hardcoded). |
| **Marathi Support** | Full Marathi STT/LLM/TTS including greeting | **PARTIAL** | `orchestrator.py:78-89`, `pipecat_bot.py:117-138` | Greeting fixed (Devanagari Marathi). Per-turn detection works. Farewell words limited. mr-IN TTS now used for greeting and responses. |
| **VAD** | Auto-detect when user starts/stops speaking | **WORKING** | `pipecat_bot.py:405-415` (SileroVADAnalyzer) | confidence=0.65, stop_secs=0.6. Works in Pipecat WebSocket pipeline only. HTTP pipeline has no VAD. |
| **Interruption** | User speaks while bot talks → bot stops | **MISSING** | Not implemented | VAD and audio playback run independently. Bot will continue speaking even if user interrupts. Captured speech is queued and processed after bot finishes. |
| **Actions** | Cancel add-on, schedule callback, send bill, recharge, raise complaint | **PARTIAL** | `agents.py` ActionAgent, `mock_server.py` | Requires mock_server.py running. All actions hit local mock endpoints — not connected to real Airtel systems. |
| **Farewell Detection** | Detect "thank you / bye" and give clean goodbye | **PARTIAL** | `agents.py:44-66`, `orchestrator.py:117-134` | False-positive risk: "done", "thanks", "no more questions" trigger goodbye mid-conversation. Substring matching, not word-boundary. |
| **Conversation History** | Maintain per-call turn history for context | **WORKING** | `orchestrator.py` `update_history()` | History filtered by language to prevent cross-language contamination. Only HTTP pipeline logs to Supabase; WebSocket pipeline does not. |
| **Call Logging (Supabase)** | Persist call records and turns in Supabase | **PARTIAL** | `supabase_client.py`, `main.py:508` | Works when configured. Mock mode logs to console. **WebSocket pipeline never calls `log_conversation_turn()`** — all real calls are unlogged. |
| **Post-escalation Q&A** | Bot continues answering after escalating | **WORKING** | `orchestrator.py:143-146` | `context.escalated = True` skips re-escalation but allows normal query path. |
| **CORS** | Allow browser to call backend from Vite dev server | **WORKING** | `main.py:57-67` | Only allows `localhost:3000` and `localhost:5173`. Production origins not configured. |

---

## 4. Known Bugs (Current)

### BUG-01: Farewell false positives (MEDIUM) — **FIXED**
**What goes wrong:** Common mid-conversation words ("done", "thanks", "no more questions", "finished") trigger the farewell short-circuit, ending the conversation prematurely.
**Where:** `agents.py:44-66` (`FAREWELL_WORDS` set), `orchestrator.py:117-134`
**Root cause:** `_is_farewell()` uses substring matching (`word in lower`), not word-boundary matching. "That's done now, but I have another question" → `"done" in lower` → True → goodbye.
**Severity:** MEDIUM — breaks natural conversations.

---

### BUG-02: WebSocket path never logs turns to Supabase (HIGH)
**What goes wrong:** All real-time calls go through `/ws/voice/{call_id}` → `OrchestratorTTSProcessor._handle_transcription()`. This never calls `supabase_client.log_conversation_turn()`. Only the HTTP `/voice/transcribe` endpoint (used in testing, not real UI) logs to Supabase.
**Where:** `pipecat_bot.py:284-365` (no Supabase call); contrast with `main.py:508`
**Root cause:** Supabase logging was added to the HTTP pipeline but not ported to the Pipecat processor.
**Severity:** HIGH — no conversation history persisted in production flow.

---

### BUG-03: `call_history` global dict is dead code (LOW) — **FIXED**
**What goes wrong:** Nothing breaks, but `call_history: dict = {}` (main.py:95) is written to nowhere and only popped in `end_call`. It is a vestige of an earlier architecture.
**Where:** `main.py:95`, `main.py:549`
**Root cause:** Architecture moved to orchestrator-managed per-call state but legacy variable not removed.
**Severity:** LOW — dead code only.

---

### BUG-04: `trigger_n8n_escalation()` in main.py is dead code (LOW) — **FIXED**
**What goes wrong:** Nothing breaks. Two escalation webhook functions exist: `main.py:613-653` defines `trigger_n8n_escalation()` which is never called. Actual escalation goes through `orchestrator.py:264-289` `trigger_escalation_webhook()`.
**Where:** `main.py:613-653`
**Root cause:** Old function never removed when escalation was refactored into the orchestrator.
**Severity:** LOW — confusing but harmless.

---

### BUG-05: `/debug/test` reports wrong TTS speaker (LOW)
**What goes wrong:** `debug/test` endpoint returns `"tts_speaker": "anushka"` in its config info (main.py:214). `anushka` is a bulbul:v2 speaker and does not exist in v3. The actual default is `female_1` → `ritu`.
**Where:** `main.py:214`
**Root cause:** Hardcoded stale string in debug response body.
**Severity:** LOW — cosmetic/debugging confusion only.

---

### BUG-06: `isCallActive` stale closure in WebSocket onclose handler (MEDIUM)
**What goes wrong:** In `startCall()`, `ws.onclose` captures `isCallActive` from the closure at the time the WebSocket was opened. If `endCall()` sets `isCallActive=false` before the socket closes, the handler may still see the old truthy value and set the status message incorrectly.
**Where:** `VoiceBot.jsx:380-385`
**Root cause:** React state captured in event handler closure becomes stale. Should use a ref or remove the conditional.
**Severity:** MEDIUM — causes UI status message flicker or incorrect state after hang-up.

---

### BUG-07: `botResponse` stale closure in `handleWsMessage` (MEDIUM) — **FIXED**
**What goes wrong:** `handleWsMessage` is memoized with `[botResponse]` as dependency. When the `audio_chunk` with `is_last=true` arrives, line 249 reads `botResponse` from state. If React hasn't batched/applied the state update from the `response` message yet, the conversation history may record an empty or stale bot response.
**Where:** `VoiceBot.jsx:248-252`, `VoiceBot.jsx:272`
**Root cause:** React `useCallback` closure captures stale state value between render cycles.
**Severity:** MEDIUM — conversation panel may show blank bot turn.

---

### BUG-08: `conversation.py detect_escalation()` is dead code (LOW) — **FIXED**
**What goes wrong:** Nothing. `ConversationManager.detect_escalation()` (conversation.py:97) is never called anywhere. Escalation logic lives entirely in `agents.py` `EscalationAgent.should_escalate()`.
**Where:** `conversation.py:97-120`
**Root cause:** Original escalation was in conversation.py; refactored to agents.py but old function left.
**Severity:** LOW — dead code only.

---

### BUG-09: RAG has no similarity threshold (MEDIUM) — **FIXED**
**What goes wrong:** Every query returns exactly 3 documents from ChromaDB regardless of relevance. A question about anything (even "hello") returns 3 documents. Irrelevant context is injected into the LLM prompt, potentially confusing responses.
**Where:** `rag_engine.py:136-141` — `query()` never checks distance scores
**Root cause:** No cosine similarity threshold applied before passing to LLM.
**Severity:** MEDIUM — degrades LLM response quality for off-topic or greeting queries.

---

### BUG-10: `MOCK_BASE` hardcoded to localhost (HIGH for any deployment) — **FIXED**
**What goes wrong:** All ActionAgent operations (`execute_action`) POST to `http://localhost:5000`. This works only when mock_server.py is running locally. Will fail silently (connection refused → fallback message) in any deployment where port 5000 is not accessible.
**Where:** `agents.py:594`
**Root cause:** No environment variable for mock server URL.
**Severity:** HIGH for non-local environments; MEDIUM in local demo.

---

### BUG-11: `ActionAgent` error fallback is always Hindi (LOW)
**What goes wrong:** When action execution fails, the error message is always in Hindi if language != "en" (agents.py:747-749). Marathi users get a Hindi error message.
**Where:** `agents.py:744-750`
**Root cause:** Only `hi` and `en` fallback strings defined; no `mr`.
**Severity:** LOW — edge case.

---

### BUG-12: ScriptProcessorNode is deprecated (LOW)
**What goes wrong:** `VoiceBot.jsx:296` uses `ctx.createScriptProcessor()` which is deprecated in all modern browsers in favour of `AudioWorklet`. Works today but may break in future Chrome/Safari updates. Also introduces ~128ms audio latency.
**Where:** `VoiceBot.jsx:296-310`
**Root cause:** Simpler API; AudioWorklet requires a separate worker file.
**Severity:** LOW — works currently, tech debt.

---

### BUG-13: `backend/nul` garbage file (LOW) — **FIXED**
**What goes wrong:** A file literally named `nul` exists in the `backend/` directory. Created on Windows when a shell command redirected output to `/dev/null` (which is `nul` on Windows and creates a file in the CWD if the path isn't found).
**Where:** `backend/nul`
**Root cause:** Windows vs Unix path difference in shell commands.
**Severity:** LOW — harmless artifact. Should be deleted.

---

### BUG-14: `chromadb==0.4.18` uses deprecated Settings API (MEDIUM)
**What goes wrong:** `rag_engine.py:36-43` instantiates ChromaDB with the legacy `Settings(persist_directory=..., is_persistent=True)` API. In ChromaDB >=0.5.x, `chromadb.Client()` with `Settings` was replaced by `chromadb.PersistentClient(path=...)`. If anyone upgrades chromadb, the import and constructor call will break.
**Where:** `rag_engine.py:36-43`
**Root cause:** Project was built against chromadb 0.4.x and frozen there.
**Severity:** MEDIUM — silent breakage risk on dependency update.

---

## 5. Language Handling Audit

### Per-Language Pipeline

#### Hindi (hi)

| Stage | What Happens |
|---|---|
| Call start | VoiceBot.jsx always sends `language: 'hi'` to `/voice/start`. Orchestrator generates Hindi greeting. |
| STT | 3-way parallel: hi-IN + en-IN + mr-IN (since call starts as hi) |
| Language detection | `_detect_language(text, user_pref='hi')` on hi-IN transcript. Devanagari density ≥33% + strong vocab match → "hi" |
| LLM system prompt | "STRICT: no CoT. आप Airtel की महिला support agent हैं। हमेशा शुद्ध हिंदी में देवनागरी लिपि में जवाब दें।" |
| LLM user message prefix | `[IMPORTANT: Customer is speaking Hindi. Respond ONLY in Hindi.]` |
| TTS | `hi-IN` → `ritu` speaker (bulbul:v3) |
| Fallback if detection fails | Defaults to `hi` (user_pref) |
| Known failure | Hindi questions with <33% Hindi word density (e.g., mostly English code-switched) may be misclassified as "en" |

#### English (en)

| Stage | What Happens |
|---|---|
| Call start | Only if UI sends `language: 'en'` — currently hardcoded to 'hi', so English calls start as Hindi |
| STT | Single en-IN STT. If Hinglish/Devanagari detected, re-transcribes with hi-IN |
| Language detection | `_detect_language(text, user_pref='en')`: if <30% Devanagari and no Hinglish/Marathi words → "en" |
| LLM system prompt | "STRICT: no CoT. You are a female Airtel support agent. Respond ONLY in English." |
| LLM user message prefix | `[IMPORTANT: Customer is speaking English. Respond ONLY in English.]` |
| TTS | `en-IN` → `ritu` speaker |
| Fallback if detection fails | Returns "en" or user_pref |
| Known failure | "What is my plan?" may be handled natively by saaras:v3 auto language detection's Hindi |

#### Marathi (mr)

| Stage | What Happens |
|---|---|
| Call start | VoiceBot.jsx sends `language: 'hi'` regardless → orchestrator uses Hindi until first Marathi turn is detected |
| Greeting | Now: Marathi Devanagari text ("नमस्कार! मी Airtel ची virtual assistant आहे.") with mr-IN TTS |
| STT | 3-way parallel (hi-IN + en-IN + mr-IN). mr-IN confirmed Marathi → use mr-IN transcript directly |
| Language detection | Marathi-specific vocab check fires FIRST (line 166 in language_detect.py): single Marathi word → "mr" |
| LLM system prompt | "STRICT: no CoT. तुम्ही Airtel ची महिला support agent आहात. फक्त मराठी Devanagari script मध्ये उत्तर द्या." |
| LLM user message prefix | `[IMPORTANT: Customer is speaking Marathi. Respond ONLY in Marathi.]` |
| TTS | `mr-IN` → `ritu` speaker (bulbul:v3) |
| Fallback if detection fails | Falls back to 'hi' (since call starts as 'hi') |
| Known failure | First greeting before user speaks is Marathi but audio TTS is mr-IN. If user hasn't spoken yet, pipecat_bot initial language reads from orchestrator (now fixed). |

---

### `_detect_language()` Deep Audit

**File:** `backend/language_detect.py:146-203`

**Algorithm:**
1. Strip Devanagari dandas (। ॥) so they don't fuse with words
2. Count Devanagari characters; compute ratio to all alpha chars
3. If ratio > 30%:
   - Check Marathi-specific vocab set → return "mr" immediately (highest priority)
   - Check Hindi vocab set for matches
   - Require: ≥2 total Hindi matches + ≥1 non-postposition "strong" match + ≥33% Hindi density → return "hi"
   - Otherwise → return "en" (Devanagari treated as STT transliteration artifact)
4. If mostly Roman script:
   - Check Marathi Roman words (`_MARATHI_ROMAN_WORDS`) → return "mr"
   - Check Hinglish words (`_HINGLISH_WORDS`):
     - ≤3 total words: 1 Hinglish hit → "hi"
     - ≥4 total words: need ≥2 Hinglish hits → "hi"
   - Otherwise → return "en"

**Edge Cases / Known Failures:**
- Very short utterances (1-2 words) of Hindi ("हाँ", "ठीक") → may pass if they're in vocab set but single-word queries are borderline
- Pure numbers ("1234") → `alpha == 0` → returns user_pref (default hi for Hindi calls)
- Pure English with a Hindi word sneaked in by STT (e.g. "what है my plan") → might classify as Hindi at low word count
- "bas" — appears in Marathi FAREWELL_WORDS but is common in many contexts

**Bootstrap lock problem:** Resolved. pipecat_bot.py now reads `initial_lang` from `orchestrator.active_calls.get(call_id)` instead of hardcoding 'hi'.

**Hinglish handling:** Correctly handled via `_HINGLISH_WORDS` frozenset. Roman Marathi distinguished via `_MARATHI_ROMAN_WORDS`. Known exclusions: "main" (English word), "ya" (yeah), "problem", "issue" removed to avoid false positives.

---

## 6. LLM Response Quality Audit

### System Prompts

**Hindi system prompt (agents.py — updated 2026-06-14):**
```
STRICT: Output ONLY the final customer response — no reasoning steps, no numbered analysis,
no chain-of-thought, no bullet points explaining your thinking.
Start your reply directly with the first word of your answer to the customer.
आप Airtel की एक महिला customer support agent हैं।
हमेशा शुद्ध हिंदी में देवनागरी लिपि में जवाब दें — Roman/Hinglish में नहीं।
यदि customer अपनी खुद की account details पूछ रहा है — जैसे 'मेरा plan', 'मेरा bill',
'मेरा data', 'मेरे add-ons' — तो CUSTOMER ACCOUNT DATA को primary source के रूप में use करें।
यदि customer general Airtel information, available options, या कोई service/feature/process
के बारे में पूछ रहा है — चाहे वो 'मुझे बताओ', 'dikhao', 'batao' जैसे words use करे —
तो REFERENCE MATERIAL को primary source के रूप में use करें।
'मुझे plans बताओ' या 'port कैसे करें' जैसे सवाल personal account questions नहीं हैं —
इनका जवाब REFERENCE MATERIAL से दें।
जब दोनों relevant हों — जैसे 'मेरे लिए कोई better plan' — तो REFERENCE MATERIAL से facts लें
और CUSTOMER ACCOUNT DATA से personalise करें।
केवल तभी Airtel Thanks app या 121 पर refer करें जब REFERENCE MATERIAL में भी answer न हो
और question के लिए real-time live data चाहिए हो जो system के पास नहीं है।
बिल amount या payment status तभी mention करें जब customer ने specifically billing के
बारे में पूछा हो — किसी और सवाल में bill amount मत बताएं।
पिछली response को दोबारा मत दोहराएं।
2-3 sentences में plain text में जवाब दें — कोई markdown नहीं।
```

**Marathi system prompt (agents.py — updated 2026-06-14):**
```
STRICT: Output ONLY the final customer response — no reasoning steps, no numbered analysis,
no chain-of-thought. Start directly with the first word of your answer.
तुम्ही Airtel ची एक महिला customer support agent आहात.
फक्त मराठी Devanagari script मध्ये उत्तर द्या — Roman नाही, Hindi नाही.
जर customer स्वतःच्या account बद्दल विचारत असेल — जसे 'माझा plan', 'माझे bill',
'माझा data', 'माझे add-ons' — तर CUSTOMER ACCOUNT DATA हा primary source म्हणून वापरा.
जर customer general Airtel माहिती, available options, किंवा कोणती service/feature/process
बद्दल विचारत असेल — जरी 'मला सांगा', 'दाखवा' असे शब्द वापरले तरी —
तर REFERENCE MATERIAL हा primary source म्हणून वापरा.
'मला plans सांगा' किंवा 'port कसे करायचे' हे personal account questions नाहीत —
यांचे उत्तर REFERENCE MATERIAL मधून द्या.
जेव्हा दोन्ही relevant असतील — जसे 'माझ्यासाठी चांगला plan' — तर REFERENCE MATERIAL मधून
facts घ्या आणि CUSTOMER ACCOUNT DATA ने personalise करा.
फक्त तेव्हाच Airtel Thanks app किंवा 121 वर refer करा जेव्हा REFERENCE MATERIAL मध्येही
उत्तर नसेल आणि real-time live data आवश्यक असेल जे system कडे नाही.
Bill amount किंवा payment status फक्त तेव्हाच सांगा जेव्हा customer ने specifically
billing बद्दल विचारले असेल — इतर कोणत्याही प्रश्नात bill amount सांगू नका.
मागील उत्तर पुन्हा सांगू नका.
2-3 वाक्यांत plain text मध्ये उत्तर द्या.
```

**English system prompt (agents.py — updated 2026-06-14):**
```
STRICT: Output ONLY the final customer response — no reasoning steps, no numbered analysis,
no chain-of-thought. Start directly with the first word of your answer.
You are a female Airtel customer support agent.
The customer is speaking English. Respond ONLY in English.
If the customer is asking about their OWN account details — such as 'my plan', 'my bill',
'my data', 'my add-ons' — use CUSTOMER ACCOUNT DATA as the primary source.
If the customer is asking about general Airtel information, available options, or how a
service or process works — even if they use 'tell me', 'show me', or 'explain' —
use REFERENCE MATERIAL as the primary source.
'Tell me about Airtel plans' or 'how do I port my number' are NOT personal account
questions — answer them from REFERENCE MATERIAL.
When both are relevant — such as 'suggest a better plan for me' — use REFERENCE MATERIAL
for facts and CUSTOMER ACCOUNT DATA for personalisation.
Only refer to the Airtel Thanks app or 121 when the REFERENCE MATERIAL also does not
contain the answer AND the question requires real-time live data the system does not have.
Do NOT mention the customer's bill amount or payment status unless they specifically asked
about billing.
Do not repeat your previous response.
Reply in 2-3 short spoken sentences, plain text only, no markdown.
```

**Default system prompt (fallback, sarvam_client.py):**
```
STRICT: Output ONLY the final customer response — no reasoning steps, no numbered analysis,
no chain-of-thought. Start directly with your answer to the customer.
You are a helpful Airtel customer support agent.
[language detection and style instructions follow]
```

**Additional reinforcement:** Every LLM call also prepends `[IMPORTANT: Customer is speaking {Language}. Respond ONLY in {Language}.]` to the user message.

**user_message context ordering (updated 2026-06-14):** REFERENCE MATERIAL now appears before CUSTOMER ACCOUNT DATA in the user message, with explicit labels on what each is for. Previously the order was reversed, which biased the LLM toward account data due to recency effects. When no RAG context is available, falls back to CUSTOMER ACCOUNT DATA only.

### CoT Defense — 4 Layers

| Layer | What It Does | Where |
|---|---|---|
| L1 — Density threshold | Hindi queries with <33% Hindi word density → classified as English → proper CoT stripping path | `language_detect.py:186` |
| L2 — `_strip_cot()` | Detects `^\d+\.\s*\*\*` CoT markers. Hindi/Marathi: extracts last ≤3 Devanagari sentences (≤300 chars each). English: backward walk, stops at numbered headers, skips analysis-prose lines (`_ANALYSIS_LINE`) | `sarvam_client.py:42-96` |
| L3 — System prompt | All 3 language prompts start with "STRICT: Output ONLY the final customer response — no reasoning steps" | `agents.py`, `sarvam_client.py` |
| L4 — `_voice_clean()` | Strips numbered lists (`1. `), markdown (`#`, `*`, `_`, `` ` ``), bullet points, table pipes before TTS | `agents.py:379-388` |

### Residual Risk
- sarvam-105b is a reasoning model. Even with explicit prohibition, it occasionally externalises reasoning. The 4-layer defense reduces but does not eliminate this risk.
- The `_strip_cot()` fast-path (`if not re.search(r'(?m)^\d+\.\s*\*\*', text): return text`) means responses without numbered CoT headers bypass cleaning entirely — if the model writes prose reasoning without numbered steps, it reaches the customer.
- `_ANALYSIS_LINE` catches "Wait,", "Actually,", "Let me", "Hmm,", "I should" — but not all possible self-correction phrasing.

---

## 7. Knowledge Base Audit

### What's in the KB (`db/airtel_kb.json`) — 22 documents

| Category | Documents | Topic |
|---|---|---|
| `prepaid_plans` | kb_001–004 | ₹199, ₹299, ₹399, ₹599 prepaid packs |
| `postpaid_plans` | kb_005–008 | ₹499, ₹799, ₹999, ₹1499 (Airtel Black) postpaid |
| `balance_check` | kb_009 | How to check balance (USSD, app, SMS, 121) |
| `data_usage` | kb_010 | How to check data balance |
| `recharge` | kb_011 | How to recharge (app, Paytm, Google Pay, store) |
| `customer_care` | kb_012 | 121 contacts, email, social media |
| `5g_service` | kb_013 | 5G availability, city list, compatible devices |
| `number_portability` | kb_014 | MNP process (SMS PORT to 1900) |
| `bill_payment` | kb_015 | Postpaid bill payment options, auto-pay, due dates |
| `international_roaming` | kb_016 | iRoam packs, activation, countries |
| `sim_replacement` | kb_017 | SIM replacement, eSIM, lost SIM, fees |
| `network_issues` | kb_018 | Troubleshooting steps, APN, airplane mode |
| `airtel_black` | kb_019 | Airtel Black family plans, pricing |
| `value_added_services` | kb_020 | Xstream, Wynk, Hellotunes, McAfee |
| `complaint_escalation` | kb_021 | Nodal officer, TRAI, consumer forum |
| `telecom_terminology` | kb_tech_terms | 5G, VoLTE, eSIM, USSD, IMEI, MNP glossary |
| `self_service` | kb_ussd_codes | All USSD codes, self-service methods |

**All KB documents are in English.** When a Hindi or Marathi query matches a KB document, the English context is appended to the LLM prompt. The LLM is expected to translate/use this internally. This works but is less precise than native-language KB entries.

### What the Bot Can Reliably Answer
- Current plan details (from customer profile, not RAG)
- Data usage and remaining data (from customer profile)
- Bill amount and due date (from customer profile)
- How to check balance / recharge (RAG)
- Plan options comparison (RAG, but fictional prices may differ from real Airtel)
- 5G availability, MNP process, SIM replacement (RAG)
- USSD codes (RAG)

### What the Bot Will Fail to Answer
- Any question about a plan not in the KB (e.g. "Do you have a ₹155 plan?")
- Real-time network outage status ("Is Airtel down in Mumbai right now?")
- Actual account-level complaint ticket status (mock DB has no resolution system)
- Broadband/DTH/Airtel Fiber queries (not in KB)
- Device-specific questions ("Why does 5G not work on my iPhone 14?")
- Region-specific pricing or promotions
- Anything requiring live Airtel system access

### RAG Retrieval
- **Embedding model:** `all-MiniLM-L6-v2` (384-dimensional, English-optimised)
- **Retrieval:** cosine similarity, top-3, similarity threshold 0.5 applied — results above threshold distance are filtered out before injection (fixed in improvement #5).
- **ChromaDB:** Local persistent store. Pre-populated on first run.
- **Context ordering (updated 2026-06-14):** REFERENCE MATERIAL is now injected before CUSTOMER ACCOUNT DATA in the LLM user message. Previously the reverse order biased the LLM toward account data even for general queries.
- **Remaining gap:** KB is English-only. Hindi/Marathi queries retrieve English docs; LLM translates internally. Works but native-language KB entries would improve precision.

---

## 8. Escalation Audit

### What Triggers Escalation

Three separate trigger conditions in `agents.py:825-866` (`EscalationAgent.should_escalate()`):

**Trigger 1 — Customer explicitly asks for human:**
Checks `ESCALATION_PHRASES` across 4 dicts: English phrases, Hindi Roman phrases, Devanagari phrases, Marathi Roman phrases. ~50+ phrases total. Examples: "talk to agent", "manager se baat", "एजेंट चाहिए", "supervisor hava".

**Trigger 2 — High-priority issue type detected:**
If `ConversationManager.classify_issue()` returns one of: `billing_dispute`, `unauthorized_charge`, `account_security`, `fraud`, `legal_threat`, `roaming_dispute` → auto-escalate.
- Example: User says "fraud" → issue_type = "fraud" → escalate immediately, even without explicit request.

**Trigger 3 — Repeat customer with open complaint:**
If `customer.complaint_history` is non-empty AND `turn_count >= 2` → escalate. Currently only Vikram Singh (9988776655) has a complaint on file.

### Routing Logic
Issue type → team:
- `billing_dispute`, `unauthorized_charge`, `roaming_dispute` → `billing_team`
- `network_issue` → `technical_team`
- `account_security` → `security_team`
- `churn_risk`, `retention` → `retention_team`
- Everything else → `general_support`

**Note:** `churn_risk` routing exists in `ROUTING_MAP` but `should_escalate()` only checks churn via the complaint-history path — there's no explicit churn-risk-based escalation trigger in the current code.

### Missing Escalation Scenarios
- **Repeated failed STT** (3 empty transcripts in a row) → no escalation trigger
- **Tone/sentiment detection** (angry customer) → not implemented
- **Long silence** → not implemented
- **Mid-call churn risk detection** → only triggers if complaint_history + 2+ turns

### Post-Escalation Behaviour
After escalation, `context.escalated = True`. Subsequent turns skip the escalation check but proceed through the normal query pipeline (ClassifyIssue → QueryResolver or ActionAgent). Bot continues to answer questions. This works correctly.

### n8n Workflow (Step-by-Step)
```
[Webhook POST /webhook/escalation]
    ↓ (receives: call_id, issue_type, user_query, bot_response, customer_name, etc.)
[Set Fields node] — extracts key fields
    ↓
[HTTP POST localhost:5000/mock/ticket] — creates mock ticket, returns ticket_id
    ↓
[HTTP POST 360dialog /v1/messages] — sends WhatsApp to pre-configured number
    ↓
[HTTP POST localhost:8000/n8n/webhook] — callback to FastAPI, updates Supabase
```

**Critical dependency:** `mock_server.py` must be running. If it's down, n8n stops at "Create Ticket" and WhatsApp is never sent.

**WhatsApp sandbox gotcha:** 360dialog sandbox only delivers to pre-approved numbers. Recipient must message the sandbox number first to opt in. Messages appear in WhatsApp "Updates" tab, not main chats.

---

## 9. Frontend Audit

### UI Components
- Header: Airtel logo, "Hindi · English · मराठी" label
- Status bar: animated status dot (Idle/Active/Recording/Processing/Speaking) + call timer
- Phone number input (pre-call): defaults to `9876543210`
- Customer badge (in-call): shows identified customer or "Guest" warning
- Start/End call buttons
- Escalation banner (appears on escalation)
- Live "You said" / "Airtel Bot" preview boxes
- Conversation history (scrollable chat bubbles, max-height 64)
- How-to instructions (shown when idle + no history)

### State Variables
| State | Reset on endCall | Notes |
|---|---|---|
| `callId` | Yes | UUID from `/voice/start` |
| `isCallActive` | Yes | Gates button display |
| `botStatus` | Yes → 'idle' | idle/active/recording/processing/speaking |
| `transcription` | Yes | Last user utterance |
| `botResponse` | Yes | Last bot response |
| `conversation` | **No** | Full history stays on screen after call ends |
| `escalated` | Yes | Escalation state |
| `ticketMessage` | Yes | Ticket ID display |
| `callDuration` | Yes | Timer |
| `customerName` | Yes | Customer name badge |
| `customerFound` | Yes | Guest vs identified |
| `error` | Yes | Auto-dismisses after 6s |

Note: `conversation` is NOT reset on endCall. This is intentional (user can review history) but may confuse users if they start a new call immediately — old history remains until a new turn arrives.

### Audio Architecture
**Capture:** Web Audio API `ScriptProcessorNode` with buffer size 2048, downsamples to 16kHz PCM16, sends as binary WebSocket frames every ~128ms.

**Playback:** `AudioQueue` class uses `AudioContext.decodeAudioData()` to decode WAV, schedules playback with `startAt = max(currentTime, nextPlayTime)` for gapless sequential chunks.

**Known Issue:** `AudioContext` is created in `startPcmCapture()` (line 283) and in `AudioQueue.init()` separately. Two AudioContexts exist simultaneously (one for capture, one for playback). This is valid but consumes more resources and could be merged.

### VAD
VAD is **server-side** (SileroVAD in Pipecat). The browser streams PCM continuously; the backend decides when speech starts/stops.

Parameters: `confidence=0.65`, `start_secs=0.2`, `stop_secs=0.6`, `min_volume=0.4`.

There is **no client-side silence detection**. The frontend always streams audio. The SileroVAD on the server buffers and segments it.

### Known UX Problems
1. **No bot-speaking gate:** VAD captures user speech even while the bot is talking. If the user speaks during bot playback, their utterance is captured, queued, and processed after bot finishes — creating a confusing "delayed" interaction.
2. **No visual "bot speaking" progress bar:** User sees "Bot is speaking..." but no indication of how long it will last.
3. **`conversation` not cleared between calls:** If user ends call and starts a new one, old conversation stays visible until new turns arrive.
4. **Phone number defaults to `9876543210`:** Non-obvious for a real demo — user might not know to change it.
5. **Language hardcoded to 'hi':** User interface says "Hindi · English · मराठी" but all calls start in Hindi. A Marathi-first user must speak before the bot detects their language.
6. **No visual microphone level indicator:** No feedback that the microphone is actually capturing audio.

---

## 10. What Is Actually Demo-Ready

### Scenarios That Would Work Reliably
1. **Hindi call with known customer (9876543210):** Start call → Hindi greeting → ask about plan, data, bill → get accurate answers → escalate → WhatsApp notification (if all services running)
2. **English call with known customer:** Start call → Hindi greeting → speak English → bot detects and responds in English. All turns.
3. **Language switch Hindi→Marathi:** Start call, speak Hindi, then switch to Marathi mid-conversation — bot follows.
4. **Action flows (Hindi):** "Cancel add-on", "send bill to WhatsApp", "raise complaint" — works with mock_server running.
5. **Escalation phrases:** "Talk to agent", "escalate karo", "एजेंट चाहिए" → escalation banner + WhatsApp notification.

### Scenarios That Would Likely Fail or Embarrass
1. **Pure Marathi from call start:** First greeting is Marathi but user has to speak first for detection to kick in. If greeting TTS sounds unnatural with mr-IN voice on mixed text, it may confuse the interviewer.
2. **Farewell mid-conversation:** Saying "that's done, thanks" or "ok, no more questions" triggers goodbye and ends the conversation prematurely (BUG-01).
3. **Off-topic or KB-gap questions:** "Is Airtel Fiber available?" or "Why is my call dropping in Pune?" — LLM will either hallucinate or give a generic answer since KB doesn't cover these.
4. **WhatsApp demo:** Requires Docker + n8n + mock_server + 360dialog sandbox opt-in + pre-approved number. Many moving parts that can fail silently.
5. **Conversation history in Supabase:** If interviewer checks Supabase dashboard, no turns will be logged (BUG-02).
6. **Long pauses:** If user pauses for >1-2 seconds mid-sentence, SileroVAD (`stop_secs=0.6`) will cut off the utterance.
7. **Network latency:** LLM takes 3-8 seconds. During that time, the UI shows "Bot is thinking..." with a spinner. If the network is slow, this becomes uncomfortable.

### Single Most Important Fix Before Demo
**BUG-01 (farewell false positives)** — the word "done", "thanks", or "no more" mid-conversation instantly terminates the call with a goodbye. This would happen naturally in a live demo (e.g., "ok done, now tell me about my data balance"). Fix: use word-boundary matching or require full utterances to be primarily farewell-intent.

---

## 11. Recommended Fix Priority

| Priority | Issue | Effort | Impact | Fix Approach |
|---|---|---|---|---|
| **P0 (Fix Now)** | BUG-01: Farewell false positives | 30 min | Prevents mid-demo crash | Use `\bword\b` regex matching; require farewell to be the entire (short) utterance; add minimum word-count check |
| **P0 (Fix Now)** | BUG-07: botResponse stale closure | 15 min | Blank bot turn in chat history | Use a ref (`botResponseRef`) alongside state; read ref in callback |
| **P1 (Fix Before Demo)** | BUG-02: No Supabase logging in WebSocket path | 45 min | Demo looks incomplete if DB checked | Add `supabase_client.log_conversation_turn()` call in `OrchestratorTTSProcessor._handle_transcription()` |
| **P1 (Fix Before Demo)** | VoiceBot.jsx:337 hardcoded `language: 'hi'` | 15 min | Marathi users get Hindi greeting audio for first turn | Pass detected/selected language; or detect from browser navigator.language |
| **P1 (Fix Before Demo)** | RAG no similarity threshold | 1 hr | LLM gets irrelevant context for greetings and off-topic queries | Add cosine distance threshold (e.g., <0.5 → discard); filter before injecting to LLM |
| **P1 (Fix Before Demo)** | BUG-06: isCallActive stale closure | 15 min | UI status flicker | Replace `if (isCallActive)` with `if (callIdRef.current)` in ws.onclose |
| **P2 (Nice to Have)** | Interruption handling (bot stops when user speaks) | 2-3 hr | Natural conversation feel | Add `isBotSpeakingRef` gate in STT; cancel queued audio chunks |
| **P2 (Nice to Have)** | Microphone level indicator | 1 hr | UX polish | Compute RMS from ScriptProcessorNode input; display animated level bar |
| **P2 (Nice to Have)** | BUG-08, BUG-03, BUG-04: Remove dead code | 30 min | Code cleanliness | Delete `detect_escalation()`, `call_history`, `trigger_n8n_escalation()` |
| **P2 (Nice to Have)** | BUG-11: Marathi error fallback in ActionAgent | 15 min | Marathi UX | Add `"mr": "..."` fallback string in `execute_action` |
| **P2 (Nice to Have)** | BUG-09: RAG similarity threshold | 1 hr | LLM response quality | Already listed under P1; same fix |
| **P3 (Post-Submission)** | BUG-12: ScriptProcessorNode deprecated | 4-6 hr | Future-proofing | Migrate to AudioWorklet; requires separate JS worker file |
| **P3 (Post-Submission)** | BUG-14: chromadb 0.4.18 legacy API | 1 hr | Dependency upgrade | Upgrade to chromadb ≥0.5, use `PersistentClient(path=...)` |
| **P3 (Post-Submission)** | BUG-13: `backend/nul` artifact | 5 min | Cleanliness | `rm backend/nul`; add to `.gitignore` |
| **P3 (Post-Submission)** | Hindi/Marathi KB documents | 4+ hr | Better RAG quality | Translate/rewrite KB documents in Hindi/Marathi for native language retrieval |
| **P3 (Post-Submission)** | MOCK_BASE as env var (BUG-10) | 15 min | Deployment | Add `MOCK_SERVER_URL` env var |
| **P3 (Post-Submission)** | Supabase CORS / production origins | 30 min | Production readiness | Add production domains to CORS allow list |

---

## Appendix: Demo Customer Reference

| Phone | Name | Segment | Churn Risk | Special Condition |
|---|---|---|---|---|
| 9876543210 | Rahul Sharma | Postpaid Premium ₹999 | Medium | 5G, Netflix+Amazon addons, bill overdue |
| 9123456789 | Priya Mehta | Prepaid ₹299 | Low | Near daily data limit |
| 9988776655 | Vikram Singh | Postpaid ₹499 | High | Open dispute on roaming charges — auto-escalates turn 2+ |
| 9876501234 | Anita Desai | Prepaid ₹199 | Low | Consistently hits 1GB/day limit |
| 9845123456 | Mohammed Raza | Postpaid ₹799 | Very High | 6-year customer, port request not initiated, network complaint history |
| (any other) | "Valued Customer" | unknown | unknown | Loads demo customer 9876543210 as fallback |

> For escalation demo: use **9988776655** (Vikram Singh) — open complaint auto-escalates after turn 2, and his bill has "dispute_raised: true" which will also trigger billing_team routing immediately.
