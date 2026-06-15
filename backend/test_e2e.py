#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
End-to-end language detection & response test for Sarvam Telecom Bot.

Objective
---------
User speaks → agent detects language → responds + transcribes in SAME language.
Mid-conversation language switches must also be detected and matched.

Test coverage
-------------
  T1  English-only (frontend starts with hi preference → switches to en after T1)
  T2  Hindi-only
  T3  Language switch: Hindi → English
  T4  Language switch: English → Hindi
  T5  Three-way: Hindi → English → Hindi

How it works
------------
  1. For each utterance, generate TTS audio (real Sarvam speech, not silence).
  2. POST that audio to /voice/transcribe exactly as the frontend would.
  3. The `language` field sent mirrors what the frontend tracks (starts 'hi',
     then updates to whatever the backend returned on the previous turn).
  4. Check: returned `language`, transcript script, response script.

Run
---
  python test_e2e.py              # starts backend automatically
  python test_e2e.py --no-server  # backend already running on :8000
"""

import argparse
import asyncio
import base64
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sarvam_client import SarvamClient

BACKEND_URL  = "http://localhost:8000"
BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
API_KEY      = os.environ.get("SARVAM_API_KEY", "")
API_BASE     = os.environ.get("SARVAM_API_BASE", "https://api.sarvam.ai")

sarvam = SarvamClient(api_key=API_KEY, base_url=API_BASE)

# -- Language helpers ----------------------------------------------------------

def detect_script(text: str) -> str:
    """Returns 'hi'/'mr' (Devanagari), 'en' (Roman), or 'mixed'."""
    deva  = len(re.findall(r"[\u0900-\u097F]", text))
    roman = len(re.findall(r"[a-zA-Z]", text))
    total = deva + roman
    if total == 0:
        return "unknown"
    ratio = deva / total
    if ratio >= 0.55:
        return "hi"
    if ratio <= 0.20:
        return "en"
    return "mixed"

def script_matches(actual_script: str, expected_lang: str) -> bool:
    if expected_lang in ("hi", "mr"):
        return actual_script in ("hi", "mixed")
    return actual_script == "en"

# -- Test conversations --------------------------------------------------------
# Each turn: text_to_speak, tts_lang, exp_detected_lang, description
# `send_lang` is tracked dynamically (starts 'hi', updates from response).

CONVERSATIONS = [
    {
        "name": "T1 — English-only (default hi → auto-switch en)",
        "phone": "9876543210",
        "turns": [
            ("how much data is left in my account", "en-IN", "en", "EN data balance"),
            ("what is my current plan",             "en-IN", "en", "EN plan query"),
            ("my bill payment status",              "en-IN", "en", "EN billing"),
        ],
    },
    {
        "name": "T2 — Hindi-only",
        "phone": "9876543210",
        "turns": [
            ("मेरा बैलेंस कितना है",  "hi-IN", "hi", "HI balance query"),
            ("मेरा प्लान क्या है",    "hi-IN", "hi", "HI plan query"),
            ("इंटरनेट स्लो है",      "hi-IN", "hi", "HI network slow"),
        ],
    },
    {
        "name": "T3 — Language switch: Hindi → English",
        "phone": "9876543210",
        "turns": [
            ("मेरा बैलेंस कितना है",             "hi-IN", "hi", "Turn1: Hindi"),
            ("how much data is left in my account", "en-IN", "en", "Turn2: EN (send=hi)"),
            ("what is my current plan",             "en-IN", "en", "Turn3: EN (send=en)"),
        ],
    },
    {
        "name": "T4 — Language switch: English → Hindi",
        "phone": "9876543210",
        "turns": [
            ("what is my current plan",  "en-IN", "en", "Turn1: English (send=hi)"),
            ("मेरा डेटा कितना बचा है", "hi-IN", "hi", "Turn2: Hindi (send=en)"),
            ("इंटरनेट बहुत स्लो है",   "hi-IN", "hi", "Turn3: Hindi (send=hi)"),
        ],
    },
    {
        "name": "T5 — Three-way: Hindi → English → Hindi",
        "phone": "9876543210",
        "turns": [
            ("मेरे प्लान की जानकारी दो",          "hi-IN", "hi", "Turn1: Hindi"),
            ("how much data do I have remaining",    "en-IN", "en", "Turn2: English"),
            ("मेरा बिल कितना है",                  "hi-IN", "hi", "Turn3: Back to Hindi"),
        ],
    },
    {
        "name": "T6 — Marathi-only (default hi → auto-switch mr)",
        "phone": "9876543210",
        "turns": [
            ("माझा balance किती आहे",  "mr-IN", "mr", "MR balance query"),
            ("माझे plan काय आहे",      "mr-IN", "mr", "MR plan query"),
            ("माझे bill किती झाले",    "mr-IN", "mr", "MR billing query"),
        ],
    },
    {
        "name": "T7 — Language switch: Hindi → Marathi",
        "phone": "9876543210",
        "turns": [
            ("मेरा बैलेंस कितना है",    "hi-IN", "hi", "Turn1: Hindi"),
            ("माझे plan काय आहे",       "mr-IN", "mr", "Turn2: Marathi (send=hi)"),
            ("माझे bill किती झाले",     "mr-IN", "mr", "Turn3: Marathi (send=mr)"),
        ],
    },
    {
        "name": "T8 — Language switch: Marathi → English",
        "phone": "9876543210",
        "turns": [
            ("माझा balance किती आहे",           "mr-IN", "mr", "Turn1: Marathi (send=hi)"),
            ("what is my current plan",          "en-IN", "en", "Turn2: English (send=mr)"),
            ("how much data is left in my account", "en-IN", "en", "Turn3: English (send=en)"),
        ],
    },
]

# -- Backend management --------------------------------------------------------

_backend_proc = None

async def backend_alive() -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BACKEND_URL}/health",
                             timeout=aiohttp.ClientTimeout(total=3)) as r:
                return r.status == 200
    except Exception:
        return False

async def wait_backend(timeout: int = 40) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if await backend_alive():
            return True
        await asyncio.sleep(1.5)
    return False

def start_backend():
    global _backend_proc
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    _backend_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--port", "8000", "--log-level", "warning"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

def stop_backend():
    global _backend_proc
    if _backend_proc:
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()
        _backend_proc = None

# -- Audio cache (avoid redundant TTS calls) -----------------------------------

_audio_cache: dict[tuple, bytes] = {}

async def get_audio(text: str, lang: str) -> bytes:
    key = (text, lang)
    if key not in _audio_cache:
        _audio_cache[key] = await sarvam.synthesize_speech(text, lang, "female_1")
    return _audio_cache[key]

# -- Single turn ---------------------------------------------------------------

async def run_turn(
    session: aiohttp.ClientSession,
    call_id: str,
    audio_bytes: bytes,
    send_lang: str,
) -> dict:
    payload = {
        "audio_base64": base64.b64encode(audio_bytes).decode(),
        "call_id":      call_id,
        "language":     send_lang,
    }
    async with session.post(
        f"{BACKEND_URL}/voice/transcribe",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=120),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
        return await resp.json()

# -- One conversation ----------------------------------------------------------

async def run_conversation(conv: dict) -> list[dict]:
    turn_results = []
    current_lang = "hi"  # Mirrors frontend languageRef — starts at 'hi'

    async with aiohttp.ClientSession() as session:
        # Start call
        r = await session.post(
            f"{BACKEND_URL}/voice/start",
            json={"customer_phone": conv["phone"], "language": "hi"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        data = await r.json()
        call_id = data["call_id"]
        print(f"  call_id={call_id[:12]}...")

        for idx, (text, tts_lang, exp_lang, desc) in enumerate(conv["turns"], 1):
            send_lang = current_lang   # What frontend sends this turn
            print(f"\n  -- Turn {idx}: {desc}")
            print(f"     text      : {text[:55]!r}")
            print(f"     tts_lang  : {tts_lang} | send_lang: {send_lang} | exp: {exp_lang}")

            try:
                audio = await get_audio(text, tts_lang)
                print(f"     audio     : {len(audio):,} bytes")

                result = await run_turn(session, call_id, audio, send_lang)

                actual_lang      = result.get("language", "?")
                transcription    = result.get("transcription", "")
                response         = result.get("response", "")
                tr_script        = detect_script(transcription)
                resp_script      = detect_script(response)

                lang_ok    = actual_lang == exp_lang
                tr_ok      = script_matches(tr_script, exp_lang)
                resp_ok    = script_matches(resp_script, exp_lang)
                turn_pass  = lang_ok and tr_ok and resp_ok

                print(f"     detected  : {actual_lang}  {'[OK]' if lang_ok else '[FAIL] (expected '+exp_lang+')'}")
                print(f"     transcript: {transcription[:60]!r}  [{tr_script}] {'[OK]' if tr_ok else '[FAIL]'}")
                print(f"     response  : {response[:70]!r}  [{resp_script}] {'[OK]' if resp_ok else '[FAIL]'}")
                print(f"     → {'PASS' if turn_pass else 'FAIL'}")

                # Simulate frontend updating languageRef
                current_lang = actual_lang

                turn_results.append({
                    "desc": desc, "pass": turn_pass,
                    "exp_lang": exp_lang, "actual_lang": actual_lang,
                    "lang_ok": lang_ok, "tr_ok": tr_ok, "resp_ok": resp_ok,
                    "transcript": transcription, "response": response,
                    "tr_script": tr_script, "resp_script": resp_script,
                })

            except Exception as exc:
                print(f"     ERROR: {exc}")
                turn_results.append({
                    "desc": desc, "pass": False,
                    "error": str(exc),
                    "exp_lang": exp_lang, "actual_lang": "error",
                })
                # Don't update current_lang on error

    return turn_results

# -- Main ----------------------------------------------------------------------

async def main(use_managed_server: bool = True) -> bool:
    print(f"\n{'='*65}")
    print("  Sarvam Telecom Bot — End-to-End Language Detection Test")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}")

    # Backend setup
    if await backend_alive():
        print("\nBackend already running on :8000 [OK]")
        managed = False
    elif use_managed_server:
        print("\nStarting backend...")
        start_backend()
        ready = await wait_backend(timeout=40)
        if not ready:
            print("ERROR: Backend did not start within 40 s")
            # Print stderr for diagnosis
            if _backend_proc:
                stderr = _backend_proc.stderr.read(2000)
                print("STDERR:", stderr.decode(errors="replace"))
            stop_backend()
            return False
        print("Backend ready [OK]")
        managed = True
    else:
        print("ERROR: --no-server set but backend not reachable")
        return False

    all_conv_results = []
    try:
        for conv in CONVERSATIONS:
            print(f"\n{'='*65}")
            print(f"  {conv['name']}")
            print(f"{'='*65}")
            try:
                results = await run_conversation(conv)
                all_conv_results.append({"name": conv["name"], "results": results})
            except Exception as exc:
                print(f"  CONVERSATION ERROR: {exc}")
                all_conv_results.append({"name": conv["name"], "error": str(exc)})
    finally:
        if managed:
            stop_backend()

    # -- Summary ---------------------------------------------------------------
    print(f"\n{'='*65}")
    print("  FINAL SUMMARY")
    print(f"{'='*65}")

    total_pass = total_fail = 0
    failures = []

    for cr in all_conv_results:
        if "error" in cr:
            print(f"  [FAIL] {cr['name']}: CONVERSATION ERROR — {cr['error']}")
            total_fail += 1
            failures.append(f"{cr['name']}: {cr['error']}")
            continue

        conv_pass = sum(1 for r in cr["results"] if r.get("pass"))
        conv_fail = sum(1 for r in cr["results"] if not r.get("pass"))
        total_pass += conv_pass
        total_fail += conv_fail
        mark = "[OK]" if conv_fail == 0 else "[FAIL]"
        print(f"  {mark} {cr['name']}: {conv_pass}/{conv_pass+conv_fail} turns")

        for r in cr["results"]:
            turn_mark = "  [OK]" if r.get("pass") else "  [FAIL]"
            detail = ""
            if not r.get("pass"):
                parts = []
                if not r.get("lang_ok", True):
                    parts.append(f"lang {r.get('actual_lang')}≠{r.get('exp_lang')}")
                if not r.get("tr_ok", True):
                    parts.append(f"tr_script={r.get('tr_script')}")
                if not r.get("resp_ok", True):
                    parts.append(f"resp_script={r.get('resp_script')}")
                if "error" in r:
                    parts.append(r["error"][:60])
                detail = "  → " + " | ".join(parts)
                failures.append(f"{cr['name']} / {r['desc']}: {' | '.join(parts)}")
            print(f"    {turn_mark} {r['desc']}{detail}")

    print(f"\n  Total: {total_pass}/{total_pass+total_fail} turns PASSED")

    if failures:
        print(f"\n  Failures to fix:")
        for f in failures:
            print(f"    • {f}")

    return total_fail == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-server", action="store_true",
                        help="Don't start backend; assume it's already running")
    args = parser.parse_args()

    success = asyncio.run(main(use_managed_server=not args.no_server))
    sys.exit(0 if success else 1)
