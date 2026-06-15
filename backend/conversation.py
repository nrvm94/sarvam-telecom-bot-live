"""
Conversation Manager
Handles escalation detection and issue classification for Airtel support calls.
"""

import logging

logger = logging.getLogger(__name__)

# Keywords that trigger escalation to a human agent
ESCALATION_KEYWORDS = [
    "dispute",
    "complaint",
    "escalate",
    "not working",
    "error",
    "problem",
    "wrong charge",
    "refund",
    "cancel",
    "port",
    "disconnect",
    "legal",
    "consumer court",
    # Hindi keywords
    "shikayat",      # complaint
    "galat charge",  # wrong charge
    "wapas",         # return/refund
    "band karo",     # stop/cancel
    "kaam nahi",     # not working
    # Marathi keywords
    "takrar",        # complaint
    "chukichi",      # wrong
    "paisa para",    # refund
    "band kara",     # stop/cancel
    "chalat nahi",   # not working
    # Marathi Devanagari
    "तक्रार",        # complaint
    "चुकीचे",        # wrong
    "परत",           # refund/return
    "बंद करा",       # stop/cancel
    "काम करत नाही",  # not working
]

# Issue classification keyword map
ISSUE_KEYWORDS = {
    "balance_check": [
        "balance", "bakaya", "kitna hai", "kitna bacha", "shillak", "kitee aahe",
        "kiti aahe", "khate", "account",
        # Devanagari (Hindi)
        "बैलेंस", "बकाया", "शेष", "बचा", "बैलन्स",
        # Devanagari (Marathi)
        "शिल्लक", "किती", "खाते",
    ],
    "plan_query": [
        "plan", "offer", "pack", "validity", "recharge", "tariff", "detail", "details",
        "yojana", "mahiti", "saang", "current plan", "my plan",
        # Devanagari (Hindi) — "जानकारी" (info) intentionally omitted; too generic.
        # "अकाउंट" (account) is in balance_check (Latin "account") so not repeated here.
        "प्लान", "ऑफर", "पैक", "वैलिडिटी", "रिचार्ज",
        "डिटेल", "डिटेल्स", "विवरण",
        # Devanagari (Marathi)
        "योजना", "माहिती", "सांग", "तपशील",
    ],
    "data_query": [
        "data", "internet", "speed", "4g", "5g", "mb", "gb", "net", "network",
        "milel", "aahe ka", "data left", "data remaining", "data bacha",
        # Devanagari (Hindi)
        "डेटा", "इंटरनेट", "स्पीड", "नेटवर्क", "नेट", "स्लो", "बचा डेटा",
        # Devanagari (Marathi)
        "नेटवर्क", "स्पीड", "डेटा",
    ],
    "billing_query": [
        "bill", "charge", "payment", "invoice", "amount due",
        "deu", "bhar",
        # Devanagari (Hindi)
        "बिल", "चार्ज", "पेमेंट", "भुगतान", "राशि", "बिल भरना", "बिल दिखाओ",
        # Devanagari (Marathi)
        "देणे", "भरणे", "रक्कम", "बिल",
    ],
    "billing_dispute": ["dispute", "wrong charge", "extra charge", "galat", "overcharge"],
    "port_request": ["port", "mnp", "number portability", "porting"],
    # High-complexity issues that map to HIGH_PRIORITY_ISSUES in agents.py
    "unauthorized_charge": ["unauthorized", "without permission", "bina bataye", "extra charge", "unexpected charge"],
    "account_security": ["hacked", "hack", "security breach", "password change", "login issue", "account access"],
    "fraud": ["fraud", "cheat", "dhoka", "scam", "fake"],
    "legal_threat": ["legal", "court", "trai", "consumer forum", "complaint file", "case", "police"],
    "roaming_dispute": ["roaming", "international", "abroad", "videsh", "foreign"],
}


class ConversationManager:
    """
    Analyses conversation turns for escalation signals and issue categorisation.
    """

    async def classify_issue(self, query: str) -> str:
        """
        Categorise the customer's issue into a predefined bucket.

        Args:
            query: Customer's transcribed utterance.

        Returns:
            Issue type string (e.g., "billing_dispute", "data_query").
        """
        q_lower = query.lower()

        for issue_type, keywords in ISSUE_KEYWORDS.items():
            for kw in keywords:
                if kw in q_lower:
                    logger.info(
                        "Issue classified | type=%s | keyword=%r | query=%r",
                        issue_type,
                        kw,
                        query[:80],
                    )
                    return issue_type

        logger.info("Issue classified | type=general_query | query=%r", query[:80])
        return "general_query"
