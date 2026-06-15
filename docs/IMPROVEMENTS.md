# Bot Improvement Backlog

All items confirmed and implemented in sprint on 2026-06-14.

---

## 1. Migrate STT: Saarika v2.5 → Saaras v3 ✅

**Why:** Saarika v2.5 is being deprecated. Saaras v3 has built-in automatic language detection, native codemix support, and better accuracy — removing dependence on the fragile `language_detect.py` vocabulary heuristics and the 3-way parallel STT approach.

**Done:** `backend/sarvam_client.py` — rewrote `transcribe_audio()` to use `saaras:v3`, returns `(transcript, detected_lang_code)` tuple. `backend/pipecat_bot.py` — replaced 120-line parallel STT block with single call. `backend/main.py` — replaced parallel STT block with single call. `language_detect` imports removed from both files.

---

## 2. Remove dead escalation function in main.py (BUG-04) ✅

**Why:** `trigger_n8n_escalation()` in `main.py` was never called. Real escalation lives in `orchestrator.py`.

**Done:** Deleted `main.py:613-653`. No other changes needed.

---

## 3. Fix botResponse stale closure — blank bot turn in UI (BUG-07) ✅

**Why:** `handleWsMessage` captured `botResponse` from state at memoization time, causing blank bot turns in the conversation panel.

**Done:** Added `botResponseRef`, synced on every `setBotResponse` call, read from ref inside callback. Removed `botResponse` from `useCallback` deps.

---

## 4. Dead code cleanup — BUG-03 & BUG-08 ✅

**Why:** `call_history` dict in `main.py` and `ConversationManager.detect_escalation()` in `conversation.py` were never used.

**Done:** Deleted both. Also removed `conversation.py`'s unused `detect_escalation()` method.

---

## 5. RAG similarity threshold + system prompt guardrails + KB expansion ✅

**Why:** Bot injected irrelevant KB docs into every query. No graceful redirect for unsupported topics. KB had gaps.

**Done:**
- `rag_engine.py` — added cosine distance threshold (0.5); results above threshold filtered out
- `agents.py` system prompts (EN/HI/MR) — added out-of-scope redirect to Airtel Thanks app / 121; added no-repetition instruction; removed conflicting "never redirect to app" instruction
- `db/airtel_kb.json` — expanded from 23 to 33 entries covering: Airtel Thanks app, WiFi calling/VoLTE, complaint tracking, DND, auto-pay, SIM block, call forwarding, postpaid→prepaid migration, data add-on packs, night data packs

---

## 6. Move MOCK_BASE to environment variable (BUG-10) ✅

**Done:** `backend/agents.py` — reads from `os.getenv("MOCK_SERVER_URL", "http://localhost:5000")`. Added to `.env.example`.

---

## 7. Delete backend/nul garbage file (BUG-13) ✅

**Done:** Deleted `backend/nul`. Added to `.gitignore`.

---

## 8. Upgrade LLM: sarvam-30b → sarvam-105b ✅

**Why:** Assignment PDF explicitly recommends `sarvam-105b`. Larger model follows system prompt instructions more reliably, reducing CoT leakage.

**Done:** `backend/sarvam_client.py` — updated model name in LLM call payload (both standard and tool-calling paths).

---

## 9. Fix farewell false positives (BUG-01) ✅

**Why:** Words like "done", "thanks", "finished" mid-sentence were triggering call end.

**Done:** `backend/agents.py` `_is_farewell()` — added word-count guard (>6 words → False) and word-boundary regex for Roman words to prevent substring matches like "done" in "undone".

---

## 10. Fix issue classifier misclassifying Hindi/Marathi bill queries ✅

**Why:** The word "करंट" (Devanagari for "current") was in the `plan_query` keyword list. Any Hindi/Marathi question containing "current" (e.g. "मेरा करंट बिल क्या है?") was misclassified as a plan query, causing the bot to return plan info instead of bill info.

**Done:** `backend/conversation.py` — removed `"करंट"` from `plan_query` keywords. English bill queries were unaffected; Hindi/Marathi bill queries now correctly reach `billing_query` classification.

---

## 11. Clear conversation history on endCall ✅

**Why:** Conversation panel retained previous call's history when a new call started.

**Done:** `frontend/src/VoiceBot.jsx` `endCall()` — added `setConversation([])`.

---

## 12. Fix LLM context priority — intent-based RAG vs account data routing ✅

**Why:** Three compounding problems caused the bot to give wrong answers for general Airtel knowledge questions (how-to, available plans, service info):

1. **Customer data bled into general queries** — system prompt said "answer directly from customer account data" as a blanket rule. Bot injected the customer's unpaid bill amount (e.g. Rs.1247) into answers about port process, SIM loss, WiFi calling — questions that have nothing to do with the customer's account.

2. **LLM ignored RAG when both contexts were present** — even when RAG retrieved the correct KB doc, the LLM prioritised customer data because the system prompt told it to. KB answers were lost.

3. **Redirect instruction too broad** — "for questions outside Airtel services, redirect to app/121" caused the LLM to deflect valid how-to questions it could have answered from the KB.

**Root cause identified via live testing** — 5 queries (3 Hindi, 2 Marathi) sent to `/voice/transcribe` showed all three failures reproducibly. Marathi prompts were less affected than Hindi.

**Done:**

`backend/agents.py` — three changes:

**(a) Intent-based priority rule added to all 3 system prompts (Hindi, Marathi, English):**
Replaced the blanket "answer from customer account data" instruction with an explicit intent rule:
- If customer asks about their OWN account details (my plan, my bill, my data) → CUSTOMER ACCOUNT DATA is primary source.
- If customer asks about general Airtel information, available options, how a service works — even with dative pronouns like 'मुझे/मला/tell me' — → REFERENCE MATERIAL is primary source.
- When both are relevant (e.g. "suggest a better plan for me") → REFERENCE MATERIAL for facts, CUSTOMER ACCOUNT DATA for personalisation.
- Key example added explicitly: "'मुझे plans बताओ' is NOT a personal account question."

**(b) Redirect rule narrowed in all 3 system prompts:**
Changed from "for questions outside Airtel services, redirect to app/121" to "only redirect when REFERENCE MATERIAL also has no answer AND the question requires real-time live data the system does not have."

**(c) Billing suppression added to all 3 system prompts:**
"Do not mention bill amount or payment status unless the customer specifically asked about billing." This prevents the LLM's helpful-but-wrong behaviour of warning about unpaid bills in every response regardless of the question topic.

**(d) user_message context ordering flipped:**
Previously: CUSTOMER ACCOUNT DATA first, then RAG. This biased the LLM toward account data due to recency.
Now: REFERENCE MATERIAL first, CUSTOMER ACCOUNT DATA second — with explicit labels on what each is for. When no RAG context is available, falls back to CUSTOMER ACCOUNT DATA only.

**Verified:** Re-ran the same 3 Hindi queries after fix — bill bleeding gone, KB content (port steps, SIM block steps, WiFi calling steps) now appears correctly in responses.
