"""
Supabase Client
Handles all database operations for the Airtel voice bot.
Falls back to mock/log mode when Supabase credentials are not configured.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class SupabaseClient:
    """
    Wraps the Supabase Python client with a graceful mock fallback.
    When real credentials are absent the client logs operations instead of
    persisting them, so the rest of the application works without a database.
    """

    def __init__(self, url: str, key: str):
        self.mock = False

        if not url or not key or url.strip() == "" or key.strip() == "":
            logger.warning(
                "Supabase not configured (url=%r, key present=%s) — using mock mode.",
                url,
                bool(key),
            )
            self.mock = True
            return

        try:
            from supabase import create_client, Client  # noqa: F401

            self._client = create_client(url, key)
            logger.info("Supabase client initialised | url=%s", url)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Failed to initialise Supabase client: %s — falling back to mock mode.",
                exc,
            )
            self.mock = True

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def insert_call(self, call_data: dict) -> dict:
        """
        Persist a new call record.

        Args:
            call_data: Dictionary with call metadata (call_id, language, etc.)

        Returns:
            Inserted record dict.
        """
        if self.mock:
            logger.info("MOCK insert_call | data=%s", call_data)
            return call_data

        try:
            result = (
                self._client.table("calls").insert(call_data).execute()
            )
            record = result.data[0] if result.data else call_data
            logger.info("Supabase insert_call OK | call_id=%s", call_data.get("call_id"))
            return record
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Supabase insert_call failed: %s", exc)
            return call_data

    async def log_conversation_turn(
        self,
        call_id: str,
        user_text: str,
        bot_text: str,
        language: str,
    ) -> bool:
        """
        Append a conversation turn to the call record.

        Args:
            call_id: UUID-style call identifier.
            user_text: Customer utterance.
            bot_text: Bot reply.
            language: Language code.

        Returns:
            True on success.
        """
        turn = {
            "timestamp": self._now_iso(),
            "user": user_text,
            "bot": bot_text,
            "language": language,
        }

        if self.mock:
            logger.info(
                "MOCK log_conversation_turn | call_id=%s | turn=%s", call_id, turn
            )
            return True

        try:
            # Fetch existing conversation array
            existing = (
                self._client.table("calls")
                .select("conversation")
                .eq("call_id", call_id)
                .execute()
            )
            current_conv = []
            if existing.data:
                current_conv = existing.data[0].get("conversation") or []

            current_conv.append(turn)

            self._client.table("calls").update(
                {"conversation": current_conv, "updated_at": self._now_iso()}
            ).eq("call_id", call_id).execute()

            logger.info(
                "Supabase log_conversation_turn OK | call_id=%s | turns=%d",
                call_id,
                len(current_conv),
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Supabase log_conversation_turn failed: %s", exc)
            return False

    async def end_call(self, call_id: str, duration_seconds: int) -> dict:
        """
        Mark a call as ended and record duration.

        Args:
            call_id: Call identifier.
            duration_seconds: Total call length in seconds.

        Returns:
            Summary dict.
        """
        summary = {
            "call_id": call_id,
            "duration_seconds": duration_seconds,
            "ended_at": self._now_iso(),
            "status": "completed",
        }

        if self.mock:
            logger.info("MOCK end_call | summary=%s", summary)
            return summary

        try:
            self._client.table("calls").update(summary).eq(
                "call_id", call_id
            ).execute()
            logger.info(
                "Supabase end_call OK | call_id=%s | duration=%ds",
                call_id,
                duration_seconds,
            )
            return summary
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Supabase end_call failed: %s", exc)
            return summary

    async def update_escalation(
        self, call_id: str, ticket_id: str, status: str
    ) -> bool:
        """
        Record escalation ticket details on the call record.

        Args:
            call_id: Call identifier.
            ticket_id: Support ticket ID from n8n workflow.
            status: Escalation status string.

        Returns:
            True on success.
        """
        update_data = {
            "ticket_id": ticket_id,
            "escalated": True,
            "escalation_status": status,
            "updated_at": self._now_iso(),
        }

        if self.mock:
            logger.info(
                "MOCK update_escalation | call_id=%s | ticket_id=%s | status=%s",
                call_id,
                ticket_id,
                status,
            )
            return True

        try:
            self._client.table("calls").update(update_data).eq(
                "call_id", call_id
            ).execute()
            logger.info(
                "Supabase update_escalation OK | call_id=%s | ticket=%s",
                call_id,
                ticket_id,
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Supabase update_escalation failed: %s", exc)
            return False
