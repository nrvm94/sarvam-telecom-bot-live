import logging
import os
import random
import time
from datetime import datetime, timezone

import aiohttp

from agents import (
    ActionAgent,
    CallContext,
    CustomerProfileAgent,
    EscalationAgent,
    GOODBYE_RESPONSES,
    QueryResolverAgent,
    _is_farewell,
)

logger = logging.getLogger(__name__)


def _script_of(text: str) -> str:
    """Return 'hi' if text is predominantly Devanagari, 'en' otherwise.

    Used to tag the bot's reply with its actual script rather than the
    detected *input* language — prevents a Hindi LLM response from being
    stored as language='en' and then polluting English-turn history.
    """
    if not text:
        return "en"
    deva = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    return "hi" if deva / len(text) > 0.25 else "en"


class VoiceOrchestrator:

    def __init__(self, sarvam_client, rag_engine, supabase_client):
        self.sarvam_client = sarvam_client
        self.rag_engine = rag_engine
        self.supabase = supabase_client
        self.customer_agent = CustomerProfileAgent()
        self.query_agent = QueryResolverAgent()
        self.action_agent = ActionAgent()
        self.escalation_agent = EscalationAgent()
        self.active_calls: dict = {}

    async def start_call(self, call_id: str, phone: str, language: str = "hi") -> dict:
        customer = self.customer_agent.load_customer(phone)

        # Demo fallback: if phone is missing/unrecognised, load the demo customer.
        if customer.get("segment") == "unknown":
            demo_phone = os.getenv("DEMO_DEFAULT_PHONE", "9876543210")
            logger.info(
                "[Orchestrator] Phone %r unrecognised — loading demo customer %r",
                phone, demo_phone,
            )
            customer = self.customer_agent.load_customer(demo_phone)
            phone = demo_phone

        context = CallContext(
            call_id=call_id,
            customer=customer,
            conversation_history=[],
            current_query="",
            language=language,
            turn_count=0,
            issue_type="unknown",
            sarvam_client=self.sarvam_client,
            rag_engine=self.rag_engine,
        )
        self.active_calls[call_id] = context

        name = customer.get("name", "Valued Customer")
        customer_found = customer.get("segment") != "unknown"
        first_name = name.split()[0] if customer_found else None

        if customer_found:
            if language == "hi":
                greeting = f"नमस्कार {first_name} जी! मैं Airtel की virtual assistant हूँ। आज मैं आपकी कैसे मदद कर सकती हूँ?"
            elif language == "mr":
                greeting = f"नमस्कार {first_name}! मी Airtel ची virtual assistant आहे. आज मी तुम्हाला कशी मदत करू शकते?"
            else:
                greeting = f"Hello {first_name}! I'm Airtel's virtual assistant. How can I help you today?"
        else:
            if language == "hi":
                greeting = "नमस्कार! मैं Airtel की virtual assistant हूँ। आज मैं आपकी कैसे मदद कर सकती हूँ?"
            elif language == "mr":
                greeting = "नमस्कार! मी Airtel ची virtual assistant आहे. आज मी तुम्हाला कशी मदत करू शकते?"
            else:
                greeting = "Hello! I'm Airtel's virtual assistant. How can I help you today?"

        plan_name = customer.get("plan", {}).get("name", "N/A") if isinstance(customer.get("plan"), dict) else "N/A"
        logger.info(
            "[Orchestrator] Call started | call_id=%s | customer=%r | found=%s | segment=%s | churn=%s | plan=%s | phone=%r",
            call_id, name, customer_found,
            customer.get("segment", "unknown"),
            customer.get("churn_risk", "unknown"),
            plan_name, phone,
        )
        return {
            "call_id": call_id,
            "customer_name": name,
            "customer_found": customer_found,
            "greeting": greeting,
            "customer_segment": customer.get("segment", "unknown"),
            "looked_up_phone": phone,
        }

    async def process_turn(self, call_id: str, transcription: str, detected_lang: str = None) -> dict:
        context = self.active_calls.get(call_id)
        if not context:
            logger.error("[Orchestrator] process_turn called with unknown call_id=%s", call_id)
            raise ValueError(f"No active call found for call_id: {call_id}")

        context.current_query = transcription
        context.turn_count += 1
        turn_start = time.time()

        # Farewell short-circuit — clean goodbye before LLM/escalation
        if _is_farewell(transcription):
            response_text = GOODBYE_RESPONSES.get(context.language, GOODBYE_RESPONSES["en"])
            self.update_history(context, transcription, response_text)
            logger.info(
                "[Orchestrator] Farewell detected | call_id=%s | lang=%s | response=%r",
                call_id, context.language, response_text,
            )
            return {
                "transcription": transcription,
                "response": response_text,
                "escalated": False,
                "routing": None,
                "priority": None,
                "ticket_id": None,
                "issue_type": "farewell",
                "turn_count": context.turn_count,
                "action_detected": None,
            }

        # Update language per-turn based on detected language
        if detected_lang:
            if detected_lang != context.language:
                logger.info(
                    "[Orchestrator] Language switch | call_id=%s | %s → %s",
                    call_id, context.language, detected_lang,
                )
            context.language = detected_lang

        logger.info(
            "[Orchestrator] Turn %d start | call_id=%s | lang=%s | query=%r",
            context.turn_count, call_id, context.language, transcription[:80],
        )

        try:
            from conversation import ConversationManager
            context.issue_type = await ConversationManager().classify_issue(transcription)
            logger.info(
                "[Orchestrator] Issue classified | call_id=%s | turn=%d | issue_type=%s",
                call_id, context.turn_count, context.issue_type,
            )
        except Exception as e:
            logger.warning("[Orchestrator] Issue classification failed, defaulting to general_query: %s", e)
            context.issue_type = "general_query"

        # Skip escalation check if already escalated — bot stays active to answer questions
        if context.escalated:
            logger.debug("[Orchestrator] Call already escalated — skipping escalation check | call_id=%s", call_id)
            escalation_result = {"escalate": False, "reason": None, "routing": None, "priority": None}
        else:
            escalation_result = await self._safe(
                self.escalation_agent.should_escalate(context),
                {"escalate": False, "reason": None, "routing": None, "priority": None},
            )
            if not escalation_result.get("escalate"):
                logger.debug(
                    "[Orchestrator] No escalation | call_id=%s | turn=%d | issue=%s",
                    call_id, context.turn_count, context.issue_type,
                )

        if escalation_result.get("escalate"):
            routing = escalation_result["routing"]
            priority = escalation_result["priority"]

            payload = self.escalation_agent.build_escalation_payload(context, routing, priority)
            ticket_id = await self.trigger_escalation_webhook(payload)
            context.escalated = True

            response_text = await self.escalation_agent.generate_escalation_response(
                routing, context.language, context.customer.get("name", ""), ticket_id, priority
            )
            self.update_history(context, transcription, response_text)

            elapsed_ms = (time.time() - turn_start) * 1000
            logger.info(
                "[Orchestrator] Turn %d ESCALATED | call_id=%s | routing=%s | priority=%s | ticket=%s | elapsed=%.0fms",
                context.turn_count, call_id, routing, priority, ticket_id, elapsed_ms,
            )
            return {
                "transcription": transcription,
                "response": response_text,
                "escalated": True,
                "routing": routing,
                "priority": priority,
                "ticket_id": ticket_id,
                "issue_type": context.issue_type,
                "turn_count": context.turn_count,
                "action_detected": None,
            }

        action_result = self.action_agent.detect_action_intent(context)
        detected_action = action_result.get("action")

        if detected_action:
            logger.info(
                "[Orchestrator] Action path | call_id=%s | action=%s | confirmed=%s | target=%r",
                call_id, detected_action, action_result.get("confirmed"), action_result.get("target", ""),
            )
        else:
            logger.debug("[Orchestrator] Query path (no action intent) | call_id=%s", call_id)

        if action_result.get("action"):
            if action_result.get("confirmed"):
                action_response = await self.action_agent.execute_action(
                    action_result["action"], action_result.get("target", ""), context
                )
                response_text = action_response["message"]
            else:
                response_text = self.action_agent.request_confirmation(
                    action_result["action"], action_result.get("target", ""), context.language
                )
        else:
            query_result = await self._safe(
                self.query_agent.run(context),
                {"response": self._fallback_response(context.language), "rag_context_used": "", "customer_context_used": ""},
            )
            response_text = query_result["response"]

        self.update_history(context, transcription, response_text)

        elapsed_ms = (time.time() - turn_start) * 1000
        logger.info(
            "[Orchestrator] Turn %d done | call_id=%s | issue=%s | action=%s | elapsed=%.0fms | response=%r",
            context.turn_count, call_id, context.issue_type,
            action_result.get("action"), elapsed_ms, response_text[:80],
        )
        return {
            "transcription": transcription,
            "response": response_text,
            "escalated": False,
            "routing": None,
            "priority": None,
            "ticket_id": None,
            "issue_type": context.issue_type,
            "turn_count": context.turn_count,
            "action_detected": action_result.get("action"),
        }

    def update_history(self, context: CallContext, user_text: str, bot_text: str):
        now = datetime.now(timezone.utc).isoformat()
        user_lang = context.language
        # Tag the assistant turn by the actual script of the response, not the
        # detected input language.  This prevents a Hindi LLM reply (a model
        # error) from being labelled language='en' and re-injected into the
        # English conversation history on the next turn.
        bot_lang = _script_of(bot_text)
        context.conversation_history.append({"role": "user", "content": user_text, "timestamp": now, "language": user_lang})
        context.conversation_history.append({"role": "assistant", "content": bot_text, "timestamp": now, "language": bot_lang})

    async def end_call(self, call_id: str, duration_seconds: int = 0) -> dict:
        context = self.active_calls.get(call_id)
        turns = context.turn_count if context else 0
        escalated = context.escalated if context else False
        try:
            self.supabase.end_call(call_id, duration_seconds)
            logger.debug("[Orchestrator] Supabase end_call recorded | call_id=%s", call_id)
        except Exception as e:
            logger.warning("[Orchestrator] Supabase end_call failed | call_id=%s | error=%s", call_id, e)
        if call_id in self.active_calls:
            del self.active_calls[call_id]
        logger.info(
            "[Orchestrator] Call ended | call_id=%s | turns=%d | duration=%ds | escalated=%s",
            call_id, turns, duration_seconds, escalated,
        )
        return {"status": "ended", "turns": turns}

    async def trigger_escalation_webhook(self, payload: dict) -> str:
        ticket_id = f"TKT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{random.randint(10000, 99999)}"
        payload["ticket_id"] = ticket_id
        webhook_url = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/escalation")
        logger.info(
            "[Orchestrator] Triggering escalation webhook | ticket=%s | routing=%s | priority=%s | url=%s",
            ticket_id, payload.get("routing"), payload.get("priority"), webhook_url,
        )
        try:
            t0 = time.time()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    elapsed_ms = (time.time() - t0) * 1000
                    resp_text = await resp.text()
                    logger.info(
                        "[Orchestrator] n8n webhook response | status=%d | elapsed=%.0fms | body=%s",
                        resp.status, elapsed_ms, resp_text[:200],
                    )
        except Exception as e:
            logger.warning(
                "[Orchestrator] n8n webhook unreachable — ticket logged locally | ticket=%s | error=%s",
                ticket_id, e,
            )
        return ticket_id

    @staticmethod
    async def _safe(coro, fallback):
        try:
            return await coro
        except Exception as e:
            logger.error("[Orchestrator] Agent call failed: %s", e, exc_info=True)
            return fallback

    @staticmethod
    def _fallback_response(language: str) -> str:
        if language == "hi":
            return "मुझे अभी आपका सवाल समझने में थोड़ी परेशानी हो रही है। कृपया दोबारा पूछें।"
        if language == "mr":
            return "मला आत्ता तुमचा प्रश्न समजण्यात अडचण येत आहे. कृपया पुन्हा विचारा."
        return "I'm having trouble understanding your query right now. Please try again."
