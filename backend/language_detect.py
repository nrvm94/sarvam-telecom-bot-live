"""
Language detection for Sarvam Telecom Bot.

Provides vocabulary-based detection of Hindi (hi), Marathi (mr), and English (en)
from both Devanagari and Roman script transcriptions, including Hinglish and
STT-generated transliterations.

Extracted from main.py so pipecat_bot.py can import without circular dependencies.
"""

import re

# ---------------------------------------------------------------------------
# Vocabulary sets
# ---------------------------------------------------------------------------

# Marathi-specific Devanagari words (not shared with Hindi)
_MARATHI_DEVANAGARI_WORDS = frozenset([
    # Original set
    "आहे", "आहेत", "माझा", "माझी", "माझे", "आणि", "मला",
    "कुठे", "आम्ही", "हवे", "तुमचा", "तुमची", "नाही", "काय",
    "कसे", "कधी", "कोण", "सांगा", "करा", "द्या", "घ्या",
    # Additional uniquely Marathi words
    "खूप",      # very/much   (Hindi uses बहुत)
    "पण",       # but         (Hindi uses लेकिन/पर)
    "म्हणजे",   # means/i.e.  (uniquely Marathi)
    "म्हणून",   # therefore   (uniquely Marathi)
    "नको",      # don't want  (uniquely Marathi)
    "आता",      # now         (Hindi uses अभी)
    "वेळ",      # time        (Hindi uses समय/वक्त)
    "जास्त",    # more/too much (Hindi uses ज्यादा)
    "किती",     # how much    (Hindi uses कितना/कितनी)
    "पाहिजे",   # need/want   (uniquely Marathi)
    "कशाला",    # for what    (uniquely Marathi)
    "आपण",      # we/you formal (Marathi 1st-person; Hindi आप is 2nd-person)
    "बघा",      # look/see    (Hindi uses देखिए)
    "ऐका",      # listen      (Hindi uses सुनिए)
    "सगळे",     # all/everyone (Hindi uses सब/सारे)
    "झाले",     # happened/done (Marathi past; Hindi uses हुआ)
    "झाली",
    "झाला",
    "मिळाले",   # got         (Hindi uses मिला)
    "मिळेल",    # will get    (Hindi uses मिलेगा)
    "माझ्या",   # my (oblique) (Hindi uses मेरे/मेरी)
    "तुमच्या",  # your (oblique)
    "आहात",     # you are     (Marathi 2nd-person; Hindi uses हैं)
])

# Postpositions that appear in _HINDI_DEVANAGARI_WORDS.
# These are "weak" signals: STT routinely appends them to transliterated English
# (e.g. "में करंट प्लान" for "in current plan").  A detection must include at
# least one NON-postposition ("strong") match to classify as Hindi.
_HINDI_POSTPOSITIONS = frozenset([
    "का", "की", "के", "को", "से", "में", "पर", "ने", "तक",
])

# Common Hindi Devanagari words used to distinguish real Hindi from
# STT-generated Devanagari transliterations of English speech.
# If NONE of these appear in Devanagari text, it is likely a transliteration
# artifact (e.g. "व्हाट इज माय करंट प्लान") rather than actual Hindi.
_HINDI_DEVANAGARI_WORDS = frozenset([
    # Pronouns
    "मैं", "मेरा", "मेरी", "मेरे", "मुझे", "मुझको",
    "आप", "आपका", "आपकी", "आपके",
    "हम", "हमारा", "हमारी", "हमारे",
    "तुम", "तुम्हारा", "वो", "वह", "उसका", "उसकी",
    "यह", "इसका", "इसकी",
    # Question words
    "क्या", "कैसे", "कैसा", "कैसी", "क्यों", "क्यूँ",
    "कब", "कहाँ", "कहां", "कितना", "कितनी", "कितने", "कौन",
    # Verb forms
    "है", "हैं", "था", "थी", "थे", "होगा", "होगी", "होंगे",
    "करना", "करें", "करो", "करता", "करती", "किया",
    "बताओ", "बताना", "बता", "बताइए",
    "चाहिए", "चाहता", "चाहती", "चाहते",
    # Negation / affirmation
    "नहीं", "नही", "मत", "हाँ",
    # Conjunctions / particles
    "और", "या", "लेकिन", "परंतु", "तो", "भी", "ही", "सिर्फ",
    "बहुत", "थोड़ा", "थोड़ी",
    # Postpositions
    "का", "की", "के", "को", "से", "में", "पर", "ने", "तक",
    # Common words
    "अभी", "पहले", "बाद", "आज", "कल",
    "अच्छा", "ठीक", "जी", "नमस्ते", "काफी", "कुछ", "कोई",
])

# Marathi-specific Roman-script words (not common in Hindi Romanization)
_MARATHI_ROMAN_WORDS = frozenset([
    "ahe", "aahe", "ahet", "maza", "mazi", "mala",
    "aani", "amhi", "kuthe", "tumcha", "tumchi",
    "sanga", "kara", "kadhi", "kon",
    # Additional Roman Marathi
    "kiti", "khup", "nako", "mhanje", "mhanun",
    "pahije", "kashala", "zale", "zali", "zala",
    "milale", "milel", "aata", "sagale", "vel",
    # High-frequency Marathi words confirmed missing from user's real phrases
    "majhya",   # my (oblique) — "majhya account madhe"
    "madhe",    # in/inside    — "account madhe", "plan madhe"
    "mazhe",    # my (nominative) — "mazhe balance"
    "tumhala",  # to you        — "tumhala sangto"
    "aahat",    # you are       — "tumhi aahat"
    "tyacha",   # his/their     — "tyacha number"
    "tyana",    # them/to them  — "tyana sanga"
    "ghari",    # at home       — "ghari aahe"
    "sangte",   # I tell (fem)  — "mi sangte"
    "sangto",   # I tell (masc) — "mi sangto"
    "asel",     # will be       — "kadhiparyant asel"
    "nasel",    # won't be      — "nasel asa nahi"
    "chalel",   # will work     — "chalel ka"
    "milel",    # will get      — already present
    "baga",     # look/see (informal) — "baga na"
    "yeto",     # will come (masc)
    "yete",     # will come (fem)
    "gela",     # went (masc)   — "gela ka"
    "geli",     # went (fem)
    "ala",      # came (masc)
    "ali",      # came (fem)
    "dya",      # give (formal) — "dya na"
    "ghya",     # take (formal)
])

_HINGLISH_WORDS = frozenset([
    "mera", "meri", "mujhe", "hamara", "hamare", "tumhara",
    "kya", "kaise", "kaisa", "kyun", "kab", "kahan", "kitna", "kitni",
    "hai", "hain", "tha", "thi", "hoga", "hogi",
    "nahi", "haan",
    "chahiye", "chahte",
    "kar", "karo", "karna", "karein", "karta", "karti",
    "bata", "batao", "batana",
    "aap", "tum", "hum", "woh", "yeh",
    "aur", "lekin", "toh", "bhi", "sirf", "bahut", "thoda",
    "abhi", "pehle", "baad",
    "accha", "theek", "bilkul", "shukriya",
    "ji",
    "mere", "tera", "teri", "uska", "uski",
    # NOTE: "main" (English: primary/chief) and "ya" (English: yeah) removed
    # NOTE: "problem" and "issue" already removed (common English words)
])


# ---------------------------------------------------------------------------
# Detection function
# ---------------------------------------------------------------------------

def _detect_language(text: str, user_pref: str = "hi") -> str:
    """Detect language per turn: hi, mr, or en.

    Uses vocabulary matching to distinguish real Hindi/Marathi from
    Devanagari transliterations that some STT models produce for English
    speech (e.g. "व्हाट इज माय करंट प्लान" for "what is my current plan").
    If Devanagari is present but contains no known Hindi/Marathi vocabulary
    words, we treat it as an STT artifact and return "en".
    """
    # Strip Devanagari dandas (। ॥) so they don't fuse with the preceding word
    # (e.g. "है।" → "है" so it can be found in the vocabulary sets).
    text = text.replace('\u0964', '').replace('\u0965', '')
    devanagari_words = re.findall(r'[\u0900-\u097F]+', text)
    devanagari = sum(len(w) for w in devanagari_words)
    alpha = len(re.findall(r'[a-zA-Z\u0900-\u097F]', text))
    if alpha == 0:
        return user_pref if user_pref in ("hi", "mr") else "hi"
    if (devanagari / alpha) > 0.3:
        # Devanagari present — check vocabulary to confirm it is real Hindi/Marathi
        deva_set = set(devanagari_words)
        if deva_set & _MARATHI_DEVANAGARI_WORDS:
            return "mr"
        hindi_matches = deva_set & _HINDI_DEVANAGARI_WORDS
        if hindi_matches:
            # "Strong" matches are non-postposition Hindi words (pronouns, verbs,
            # conjunctions).  Postpositions alone are not reliable — STT appends
            # them to transliterated English ("में करंट प्लान", "प्लान का").
            strong_matches = hindi_matches - _HINDI_POSTPOSITIONS
            if len(deva_set) <= 3:
                # Short utterances: require ≥1 strong (non-postposition) match.
                if strong_matches:
                    return "hi"
            else:
                # Longer utterances (4+ Devanagari words): require ≥2 total
                # matches, ≥1 strong (non-postposition) match, AND ≥40% of all
                # Devanagari words must be Hindi vocab.  The density filter rejects
                # STT transliterations where the model semantically substituted
                # just 2-3 function words ("is"→"है", "in"→"में") in an otherwise
                # English sentence.
                hindi_density = len(hindi_matches) / len(deva_set)
                if len(hindi_matches) >= 2 and strong_matches and hindi_density >= 0.33:
                    return "hi"
        # Devanagari with no recognised vocabulary (or low Hindi ratio) →
        # STT transliteration of English speech.
        return "en"
    # Roman script — check for Marathi then Hinglish.
    # Strip non-alpha chars so "aahe?" matches "aahe", "ahe." matches "ahe", etc.
    words = {re.sub(r'[^a-z]', '', w) for w in text.lower().split()} - {''}
    if words & _MARATHI_ROMAN_WORDS:
        return "mr"
    hinglish_hits = words & _HINGLISH_WORDS
    if hinglish_hits:
        # For short queries (≤3 words), even a single Hinglish word is strong enough signal.
        # For longer queries (4+ words), require ≥2 Hinglish words to avoid classifying
        # English sentences as Hindi when STT appends a single Hindi particle (e.g. "hai").
        if len(words) <= 3 or len(hinglish_hits) >= 2:
            return "hi"
    return "en"
