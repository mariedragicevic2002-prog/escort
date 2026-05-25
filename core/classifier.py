"""

Intent Classifier - Pattern-based classification with AI fallback.
Safety-critical intents use pattern matching ONLY.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
from typing import Any

logger = logging.getLogger("escort_chatbot.classifier")

_DINNER_ENQUIRY_PRIMARY_RE = re.compile(
    r"\b(dinner|dinner date|take you out|take you to dinner|free for dinner|dinnernat)\b"
)
_DINNER_ENQUIRY_TAKE_YOU_RE = re.compile(r"\b(love to|want to|keen to|like to)\s+take you\b")
_SPECIAL_BOOKING_AMBIGUITY_RE = re.compile(
    r"\b("
    r"overnite|overnight|all\s*(?:night|nite)|dirty\s*weekend|weekend\s*(?:booking|package|away)|wknd|"
    r"fly\s*me\s*(?:to|2)\s*you|fmty|travel\s*(?:with|to)\s*(?:you|her)|"
    r"filming|video\s*(?:shoot|session)|content\s*(?:shoot|session)|record(?:ing|ed)?\s*session"
    r")\b",
    re.IGNORECASE,
)


def _collecting_dinner_booking(state: dict | None) -> bool:
    """True when SMS flow is collecting dinner fields (not a brand-new enquiry)."""
    if not state:
        return False
    if (state.get("current_state") or "").strip().upper() != "COLLECTING":
        return False
    bt = (state.get("booking_type") or "").strip().lower()
    if bt == "dinner_date":
        return True
    exp = (state.get("experience_type") or "").strip().lower()
    return exp in ("dinner date", "dinner_date")


def _dinner_new_enquiry_opener(message_lower: str) -> bool:
    """
    True if the client is clearly opening or repeating a dinner *enquiry* (keep dinner_date_enquiry).

    Venue-only follow-ups like 'lets go to X restaurant' match rest(?:aurant|urant) and must not
    win dinner_date_enquiry over provide_field.
    """
    if _DINNER_ENQUIRY_PRIMARY_RE.search(message_lower):
        return True
    if _DINNER_ENQUIRY_TAKE_YOU_RE.search(message_lower):
        return True
    return False


def _looks_like_special_booking_candidate(message_lower: str) -> bool:
    """Broad, ambiguity-tolerant candidate filter before hybrid special-booking detection."""
    return bool(_SPECIAL_BOOKING_AMBIGUITY_RE.search(message_lower or ""))


def _compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern) for pattern in patterns]


def _matches_any(text: str, patterns: list[str] | list[re.Pattern[str]]) -> bool:
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            if pattern.search(text):
                return True
            continue
        if re.search(pattern, text):
            return True
    return False


# Explicit MMF/opening signals: these should be treated as a doubles MMF intent path.
# NOTE: \bdp\b removed — too broad (matches "deposit payment", initials, etc.)
# Use contextual dp patterns in INTENT_RULES["doubles_enquiry"] only.
_DOUBLES_MMF_PATTERNS = [
    r"\bmmf\b",
    r"\bmfm\b",
    r"\b(?:i|me)\s+and\s+(?:my|a)\s+(?:mate|friend|best friend|best mate)\b",
    r"\bmy\s+(?:mate|friend|best friend|best mate)\s+and\s+(?:i|me)\b",
    r"\b(?:2|two)\s+(?:guys?|blokes?)\b",
    r"\bdouble\s*team(?:ing)?\b",
    r"\bdouble\s+penetration\b",
    r"\bgang\s*bang\b",
    r"\bgangbang\b",
    r"\bspit\s*roast\b",
    r"\bspitroast\b",
    r"\bpig\s+on\s+the\s+spit\b",
    r"\bdevil'?s\s+(?:three[- ]way|tricycle)\b",
    r"\bsandwich(?:ing|n)?\b",
    r"\b(?:2|two)\s+(?:cocks?|dicks?)\b",
    r"\b(?:2|two)\s+(?:cocks?|dicks?)\s+(?:inside|at\s+the\s+same\s+time)\b",
    r"\bfill(?:ing)?\s+up\s+both\s+(?:of\s+)?your\s+holes\b",
    r"\busing\s+both\s+(?:of\s+)?your\s+holes\b",
]

# Explicit MFF/opening signals.
_DOUBLES_MFF_PATTERNS = [
    r"\bmff\b",
    r"\bffm\b",
    r"\bfmf\b",
    r"\b(?:2|two)\s+girls?\b",
    r"\b(?:2|two)\s+women\b",
    r"\b(?:2|two)\s+females?\b",
]

# Generic threesome language that needs clarification unless explicit MMF/MFF context is present.
# NOTE: \bdoubles?\b and \btrio\b removed here — too broad (match "double room", "jazz trio", etc.)
# Bare/contextual doubles for clarification live in _DOUBLES_BOOKING_CONTEXT_PATTERNS and INTENT_RULES.
_DOUBLES_AMBIGUOUS_PATTERNS = [
    r"\bthreesomes?\b",
    r"\bthree\s*some\b",
    r"\bthree\s*somes\b",
    r"\bthreesum\b",
    r"\bthreesums\b",
    r"\b3\s*sum\b",
    r"\b3\s*sums\b",
    r"\b3\s*some\b",
    r"\b3\s*somes\b",
]

# Contextual doubles wording without MMF/MFF — needs MMF vs MFF clarification.
# Kept narrow (see INTENT_RULES["doubles_enquiry"]) — no bare \bdoubles\b (double room, etc.).
_DOUBLES_BOOKING_CONTEXT_PATTERNS = [
    r"\bdoubles?\s+(?:booking|session|experience|threesome)\b",
    r"\b(?:book(?:ing)?|want|have)\s+a?\s*doubles?\b",
]

_DOUBLE_BOOKING_SLOT_RE = re.compile(
    r"\bdouble\s+book(?:ing)?\b.*\b(?:same|slot|time)\b|\bsame\s+(?:slot|time)\b.*\bdouble\s+book(?:ing)?\b",
    re.IGNORECASE,
)

# Partner/couples indicators (these belong to couples flow, not doubles ambiguity).
_COUPLES_PARTNER_PATTERNS = [
    r"\bmy\s+(?:partner|husband|wife|boyfriend|girlfriend|missus|mrs|hubby|fiance[e]?)\b",
    r"\bme\s+and\s+my\s+(?:partner|husband|wife|boyfriend|girlfriend|missus|mrs|hubby|fiance[e]?)\b",
    r"\bmy\s+(?:partner|husband|wife|boyfriend|girlfriend|missus|mrs|hubby|fiance[e]?)\s+and\s+(?:i|me)\b",
    r"\bcouples?\b",
]


def classify_doubles_signal(message_lower: str) -> str:
    """
    Return doubles signal subtype:
    - mmf_explicit: clear MMF language
    - mff_explicit: clear MFF language
    - ambiguous_threesome: generic threesome / contextual doubles booking wording needing clarification
    - none: no doubles signal
    """
    text = (message_lower or "").strip().lower()
    if not text:
        return "none"

    if _matches_any(text, _COMPILED_DOUBLES_MMF_PATTERNS):
        return "mmf_explicit"
    if _matches_any(text, _COMPILED_DOUBLES_MFF_PATTERNS):
        return "mff_explicit"
    couples_hit = _matches_any(text, _COMPILED_COUPLES_PARTNER_PATTERNS)
    if (
        _matches_any(text, _COMPILED_DOUBLES_BOOKING_CONTEXT_PATTERNS)
        and not couples_hit
    ):
        return "ambiguous_threesome"
    if _matches_any(text, _COMPILED_DOUBLES_AMBIGUOUS_PATTERNS) and not couples_hit:
        return "ambiguous_threesome"
    return "none"


# Intent definitions
INTENTS = [
    "greeting",
    "book_appointment",
    "provide_field",  # Providing date/time/duration info
    "quick_booking",  # Quick booking shortcuts (book my usual, same as last time, next available)
    "couples_booking",
    "ask_availability",
    "available_now",
    "ask_rates",
    "pricing_inquiry",
    "request_outcall",
    "confirm_booking",
    "cancel_booking",
    "reschedule",
    "modify_booking",
    "deposit_query",
    "refuse_deposit",
    "deposit_screenshot",  # Uploading deposit proof
    "resend_link",  # Request to resend upload/webform link
    "doubles_enquiry",
    "service_inquiry",
    "touring_inquiry",
    "touring_subscribe",
    "unsafe_request",  # SAFETY - pattern matching ONLY
    "rude_abusive",    # SAFETY - pattern matching ONLY
    "overnight_enquiry",
    "dinner_date_enquiry",
    "msog_enquiry",
    "location_enquiry",
    "rate_negotiation",
    "timewaster",
    "flirt",
    "goodbye",
    "enquiry_keyword",
    "wrong_number_opt_out",
    "other",
]

# Never ask the LLM to emit these — they are enforced by pattern / admin rules only.
_INTENTS_EXCLUDED_FROM_LLM = frozenset({
    "unsafe_request",
    "rude_abusive",
    "enquiry_keyword",
    "wrong_number_opt_out",
})

# Intents the LLM router may return (subset of INTENTS).
INTENTS_FOR_LLM = [i for i in INTENTS if i not in _INTENTS_EXCLUDED_FROM_LLM]

# One-line hints for the JSON intent router (keep in sync with router_registration handlers).
INTENT_DESCRIPTIONS: dict[str, str] = {
    "greeting": "Hello/hi/hey/wave — opening a conversation without a specific request.",
    "book_appointment": "Wants to book, schedule, or make an appointment (general).",
    "provide_field": "Answering a booking question: date, time, duration, incall/outcall, GFE/PSE, or short follow-ups after the bot asked.",
    "quick_booking": "Shortcut booking: usual/regular/repeat last time, next available, earliest slot.",
    "couples_booking": "Booking with a partner/spouse/couple experience (two clients together).",
    "ask_availability": "When are you free / availability for a future day or time (not urgent right now).",
    "available_now": "Wants to meet ASAP, right now, in minutes, or immediately — urgent availability.",
    "ask_rates": "Asking for rates, prices, menu, or how much (general).",
    "pricing_inquiry": "Specific price question: how much for X hours or a named service length.",
    "request_outcall": "Wants outcall: me visiting their place/hotel/address, or asking you to travel/come to them. Location-based request only — NOT a sexual act request.",
    "confirm_booking": "Confirming a booking: yes, okay, lock it in, book it.",
    "cancel_booking": "Cancelling or cannot make the appointment.",
    "reschedule": "Wants to reschedule a confirmed booking to a new date/time.",
    "modify_booking": "Reschedule or change date/time of an existing booking.",
    "deposit_query": "Questions about deposit amount, why deposit, how to pay deposit.",
    "refuse_deposit": "Refusing or unwilling to pay a deposit.",
    "deposit_screenshot": "Sending or referring to deposit/payment proof or screenshot.",
    "resend_link": "Asks to resend booking, upload, or webform link.",
    "doubles_enquiry": "Doubles/threesome/MMF/MFF/group — more than two people in the session.",
    "service_inquiry": "What services do you offer / what is included / menu of services.",
    "touring_inquiry": "Touring/travel: when in another city, return dates, tour schedule.",
    "touring_subscribe": "Reply to subscribe to tour notifications (e.g. TOURING, yes notify).",
    "overnight_enquiry": "Overnight, all night, dirty weekend, fly-me-to-you, long multi-hour stay.",
    "dinner_date_enquiry": "Dinner date, meal, restaurant, take you out, social time before/after.",
    "msog_enquiry": "MSOG, multiple rounds, cum more than once.",
    "location_enquiry": "Where are you located / which suburb / incall address area.",
    "rate_negotiation": "Asking for discount, cheaper, negotiate, lower price.",
    "timewaster": "Demands free trial, sample, first time free, test drive.",
    "flirt": "Compliments, flirting, hot/sexy/beautiful, explicit sexual act requests (deepthroat, blowjob, etc.) — not a booking or location step. Any message asking for a specific sexual act goes here.",
    "goodbye": "Bye, goodbye, talk later, see you (closing).",
    "enquiry_keyword": "Message begins with ENQUIRY — personal question for the provider.",
    "wrong_number_opt_out": "Wrong number / mistaken contact / not meant for this thread.",
    "other": "None of the above fits, or unclear/off-topic small talk.",
}

# Rule-based patterns (fast path - catches 80%+ of messages)
# Order matters! More specific patterns come BEFORE broader ones

INTENT_RULES = {
    # SAFETY — evaluated BEFORE all booking patterns in classify() safety loop.
    # rude_abusive: directed insults or threats at the bot/service.
    "rude_abusive": [
        r"\bgo\s+fuck\s+(your|ur)self\b",
        r"\bget\s+fucked\b",
        r"\bpiss\s+off\b",
        r"\bfuck\s+off\b",
        r"\bfuck(ing)?\s+(useless|stupid|dumb|pathetic)\b",
        r"\b(stupid|useless|dumb|fucking|braindead)\s+(bot|service|system|machine)\b",
        r"\b(you'?re?|ur)\s+(a\s+)?(stupid|useless|dumb|fucking|braindead)\s+(bot|thing|machine|service)\b",
        r"\bwaste\s+of\s+(fucking\s+)?(?:time|space)\b.*\b(bot|service|system|you)\b",
        r"\b(i('?ll|'?m going to)|gonna|going to)\s+(hurt|kill|bash|smash|destroy)\b",
        r"\bpathetic\b.*\b(service|bot|system)\b",
        r"\byou\s+suck\b",
        r"\bu\s+suck\b",
    ],
    # unsafe_request: illegal or coercive service requests.
    "unsafe_request": [
        r"\bno\s+condom\b",
        r"\bbareback\b",
        r"\bbbb\b",
        r"\braw\b.*\b(sex|fuck|inside|anal)\b",
        r"\bwithout\s+(a\s+)?condom\b",
        r"\bunder\s+(18|16|age)\b",
        r"\b(minor|underage)\b",
        r"\bno\s+limits?\b",
        r"\buntil\s+i\s+(finish|cum|come)\b",
        r"\bforce\b.*\b(you|her)\b.*\b(sex|do it)\b",
    ],

    # BOOKING MODIFICATION
    "cancel_booking": [
        r"\bcancel\b", r"\bcancelling\b", r"\bcancelled\b",
        r"\bcan'?t make it\b", r"\bhave to cancel\b",
        r"\bwon'?t be able\b", r"\bsomething came up\b",
        r"\bchange of plans\b", r"\brain check\b",
        # Informal / colloquial cancellations
        r"\bforget it\b", r"\bforgetting it\b",
        r"\bnever mind\b", r"\bnevermind\b",
        r"\bdon'?t bother\b",
        r"\bscratch that\b",
        r"\bnot anymore\b",
        r"\bnot interested\b",
        r"\bnot going ahead\b",
        r"\bnot happening\b",
        r"\bleave it\b",
        r"\bpull(?:ing)? out\b",
        r"\bon second thoughts?\s+(no|not)\b",
        r"\bactually\s+(no|not interested|forget)\b",
    ],
    "modify_booking": [
        r"\bchange\b.*\b(time|date)\b", r"\bpostpone\b",
        r"\bdifferent (time|date)\b", r"\bmove.*booking\b"
    ],
    "reschedule": [
        r"\breschedule\b", r"\brescheduled\b",
        r"\bchange\s+(it\s+)?to\b", r"\bmove\s+(it\s+)?to\b",
        r"\bdifferent\s+(time|day|date)\b",
    ],

    # CONFIRMATION
    "confirm_booking": [
        r"^yes$", r"^yep$", r"^yeah$", r"^yup$", r"^ok$", r"^okay$",
        r"\byes\b.*(confirm|book|please)\b",
        r"^book it$", r"\block it in\b", r"\bconfirm(ed)?\b",
        r"\b(let'?s|lets) do it\b", r"\bsee (you|u) then\b",
        r"^(gfe|pse|dgfe)\s+yes$",
        r"^yes\s+(gfe|pse|dgfe)$",
        # Affirmative acknowledgements (route to provide_field in COLLECTING via guard)
        r"\bsounds good\b", r"\bsounds great\b", r"\bsounds perfect\b",
        r"\ball good\b", r"\ball set\b", r"\bno worries\b",
        r"^perfect$", r"^great$", r"^awesome$",
        # Emoji confirmations
        r"^\U0001F44D+$",  # \U0001F44D
        r"^\u2705+$",      # \u2705
    ],

    # DEPOSIT
    "deposit_screenshot": [
        # This will be combined with media_url check
        r"\bhere('s| is)\b.*(deposit|payment|screenshot|proof)\b",
        r"\bsent\b.*(deposit|payment|screenshot)\b",
        r"\bpaid\b", r"\btransferred\b", r"\bdone\b.*\bdeposit\b"
    ],
    "refuse_deposit": [
        r"\bno\b.*\bdeposit\b", r"\bwon'?t\b.*\bdeposit\b",
        r"\bcan'?t\b.*\bdeposit\b", r"\brefuse\b.*\bdeposit\b",
        r"\bno need\b.*\bdeposit\b", r"\bskip\b.*\bdeposit\b"
    ],
    "deposit_query": [
        r"\bdeposit\b.*(how|why|what|when)\b",
        r"\b(how|why|what|when)\b.*\bdeposit\b",
        r"\bhow much\b.*\bdeposit\b",
        r"\bdeposit\b.*\bhow much\b",
    ],

    # AVAILABILITY
    "available_now": [
        # Immediate availability requests - MUST check calendar (atm = at the moment)
        # STRICT: Only urgent/immediate requests
        r"\b(right now|right away|soon|asap|immediately|straight away|straight up)\b",
        # More immediate keywords
        r"\b(as soon as possible|this very second|right this second)\b",
        r"\bash\b", r"\brly\b",  # ash (assh), rly (really bad wait)
        # Only match "now|rn|atm" with "available|free" - NOT "tonight|today" (those are ask_availability)
        r"\b(are you|are yu|you|yu|r u|u) (available|free).*(now|rn|asap|atm|immediately|right away|soon)\b",
        r"\bare yu (available|free)\b.*(now|rn|asap|atm|immediately|right away|soon)\b",
        r"\b(available|free).*(now|right now|rn|asap|atm|immediately|right away|soon|straight away)\b",
        r"\b(now|rn|atm|immediately|right away|soon|straight away)\b.*(available|free)\b",
        # "Can you meet right now?" / "Can i see you now?" / "Is it possible to see you now?"
        r"\b(is it possible|can you|can i|can we).*(see you|meet).*(now|atm|right now|immediately|soon)\b",
        r"\b(see you|meet).*(now|atm|right now|immediately|soon)\b",
        # "Are you available at this present time?" / "are you available as of now?"
        r"\bavailable.*(at this present time|as of now)\b",
        r"\b(as of now|at this present time)\b",
        # "are you busy atm?" = are you free right now?
        r"\bare you busy\b.*(now|atm|right now|soon)\b",
        r"\b(now|atm|right now|soon).*\bbusy\b",
        # "is now a good time for a booking?"
        r"\b(is |is now ).*good time.*(for a )?booking\b",
        r"\bnow.*good time\b",
        # "in 30 mins", "in 1 hour" (urgent future availability -- only short lead times)
        r"\bin\s+[1-5]?\d\s+(minutes?|mins?)\b",
        r"\bin\s+1\s+(hours?|hrs?)\b",
        # Outcall + now: "can you come to my place now?", "hi its Bob can you come to my place now?"
        r"\b(can you come|can i come|come to (me|my)|come (and )?see me|my place|visit me)\b.*\b(now|right now|atm|immediately|soon)\b",
        r"\b(now|right now|atm|immediately|soon)\b.*\b(can you come|come to (me|my)|come (and )?see me|my place|visit me)\b",
    ],
    # COUPLES BOOKING — must come BEFORE ask_availability so "are you available for a couples booking"
    # hits this intent, not the generic availability handler.
    "couples_booking": [
        r"\bcouples?\s+booking\b", r"\bcouples?\s+experience\b",
        r"\bcouples?\s+session\b",
        # "me and my X" / "my X and me/I"
        r"\bme\s+and\s+my\s+(?:partner|husband|wife|boyfriend|girlfriend|mrs|missus|hubby|fiance[e]?)\b",
        r"\bmy\s+(?:partner|husband|wife|boyfriend|girlfriend|mrs|missus|hubby|fiance[e]?)\s+and\s+(?:i|me)\b",
        r"\bmy\s+(?:better\s+half|significant\s+other)\s+and\s+(?:i|me)\b",
        r"\b(?:i|me)\s+and\s+my\s+(?:partner|husband|wife|boyfriend|girlfriend|mrs|missus|hubby|fiance[e]?)\b",
        # "bring my partner/wife/gf/husband"
        r"\bbring(?:ing)?\s+my\s+(?:partner|husband|wife|boyfriend|girlfriend|mrs|missus|hubby|fiance[e]?)\b",
        r"\bwith\s+my\s+(?:partner|husband|wife|boyfriend|girlfriend|mrs|missus|hubby|fiance[e]?)\b",
        # Standalone relationship mentions (broad, must stay after more specific ones above)
        r"\bmy\s+(?:husband|wife|partner|boyfriend|girlfriend|missus|mrs|hubby|fiance[e]?|better\s+half|significant\s+other)\b",
        # We/us booking language
        r"\bwe\s+(?:want|would\s+like|'d\s+like)\s+to\s+book\b",
        r"\bwe\s+(?:want|would\s+like|'d\s+like)\s+to\s+(?:see|meet)\s+you\b",
        r"\bboth\s+of\s+us\b",
        r"\bfor\s+(?:the\s+)?two\s+of\s+us\b", r"\bfor\s+both\s+of\s+us\b",
    ],

    # DINNER DATE — must come BEFORE ask_availability so "take me out for dinner when are you free"
    # hits this intent, not the generic "are you free" availability handler.
    # (Single dict key only — a duplicate "dinner_date_enquiry" later in this dict would overwrite
    # these patterns and could reorder the key after ask_availability, breaking dinner detection.)
    # NOTE: wine/drinks/meal/restaurant removed from here — too broad as standalone patterns.
    # They require a booking co-signal and are handled in-loop with a context guard.
    "dinner_date_enquiry": [
        # Typo / merged words: "free for dinnernat 8pm" has no \bdinner\b boundary — match prefix.
        r"\bfree for dinner",
        r"\bdinner[a-z]{2,}\b",
        r"\bdinner\b", r"\bdinner date\b", r"\btake you out\b",
        r"\bsocial time\b",
        r"\beat.*together\b", r"\bgrab (a )?bite\b",
        r"\btake you (out )?for dinner\b",
        r"\bdinner (and|then|before) (fuck|sex|fucking)\b",
        r"\bfuck(ing)? (after|afterwards)\b.*\bdinner\b",
        r"\b(come to|at) my place.*\b(cook|dinner|feed you)\b",
        r"\bcook (for )?you dinner\b",
        r"\bfeed you dinner\b",
        r"\btake.*out (for|to) (dinner|a meal|eat)\b",
        r"\b(wanna|want to|keen to|like to|love to) take you out\b",
        # Social venue words ONLY when paired with dinner/date/booking context
        r"\b(wine|drinks|meal|rest(?:aurant|urant|uraunt))\b.*\b(dinner|date|take you|book|reservation)\b",
        r"\b(dinner|date|take you|book|reservation)\b.*\b(wine|drinks|meal|rest(?:aurant|urant|uraunt))\b",
    ],

    "ask_availability": [
        # General availability questions (no urgency) - show 3-slot template
        r"\bwhen.*available\b", r"\bavailability\b",
        r"\bwhat.*(day|time).*available\b",
        r"\bcan (i|you) (book|see)\b",
        # Plain asks like "are you free?" / "you available?"
        r"\b(are you|you|r u|u)\s+(available|free)\b",
        r"\b(are you|you|r u|u) (available|free)\s*(tomorrow|next week|this week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        # "available/free" + day or time (e.g. "are you available tomorrow at 8pm", "free tomorrow evening")
        r"\b(are you|you|r u|u)\s+(available|free)\b.*\b(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(pm|am))\b",
        r"\b(available|free)\s+(tomorrow|tonight|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b(available|free)\s+.*\b\d{1,2}\s*(pm|am)\b",
        # "are you free tonight/today by any chance?" - general inquiry without immediate urgency
        r"\b(are you|you|r u|u)\s+(available|free)\b.*(tonight|today|this evening|this afternoon)\b",
    ],

    # PRICING
    "pricing_inquiry": [
        r"\bhow much.*(for|is)\b", r"\bwhat.*(price|cost|rate)\b.*\b(for|is)\b",
        r"\b\d+ hour.*(cost|price|rate)\b",
        r"\bprice for\b", r"\bcost of\b"
    ],
    "ask_rates": [
        r"\b(rate|rates|price|prices|cost|how much|pricing)\b",
        r"\$\d+", r"\bwhat do you charge\b",
        r"\U0001F4B0", r"\U0001F4B5"  # Money emojis
    ],
    "rate_negotiation": [
        r"\bnegotiate\b", r"\bdiscount\b", r"\blower\b.*\bprice\b",
        r"\bcheaper\b", r"\bbetter\b.*\brate\b",
        r"\b(can|will) you.*\b(lower|reduce)\b"
    ],

    # SPECIAL BOOKING TYPES — must come BEFORE book_appointment so "book a doubles/overnight/etc."
    # doesn't get swallowed by the broad r"\b(book|booking)\b" pattern.

    "doubles_enquiry": [
        # Booking type terms (threesome/group explicit — NOT couples with partner)
        # bare \bdoubles?\b and \btrio\b with negative lookahead to block non-sexual meanings
        r"\bdoubles?\b(?!\s*(?:room|bed|check|decker|deck|park|bass|shot|espresso|vision|glazed|sided|blind|edged|lock|entry|dutch|barrel|standard|size|booking\b))",
        r"\btrio\b(?!\s*(?:sonata|concerto|pack|set|meal|deal|package|option))",
        r"\bthreesome\b", r"\bthreesum\b",
        r"\b3\s*sum\b", r"\bmmf\b", r"\bmfm\b", r"\bffm\b", r"\bfmf\b",
        r"\bgroup\s+(?:booking|sex|session)\b",
        r"\borgy\b", r"\bswingers?\b", r"\bswinging\b",
        r"\bfoursome\b", r"\b4\s*some\b",
        r"\btag\s*team\b", r"\bdouble\s*team\b",
        r"\bspit\s*roast\b", r"\bspitroast\b",
        r"\bdouble\s+penetration\b",
        r"\bgangbang\b", r"\bgang\s+bang\b",
        r"\b2\s+on\s+1\b", r"\btwo\s+on\s+one\b",
        r"\bpig\s+on\s+the\s+spit\b",
        r"\bdevil'?s\s+(?:three[- ]way|tricycle)\b",
        r"\bsandwich(?:ing)?\b",
        r"\b(?:2|two)\s+(?:cocks?|dicks?)\b",
        r"\b(?:2|two)\s+(?:cocks?|dicks?)\s+(?:inside|at\s+the\s+same\s+time)\b",
        r"\bfill(?:ing)?\s+up\s+both\s+(?:of\s+)?your\s+holes\b",
        r"\busing\s+both\s+(?:of\s+)?your\s+holes\b",
        # Multiple people indicators
        r"\btwo.*girls?\b", r"\banother.*girl\b",
        r"\b2\s+guys?\b", r"\btwo\s+guys?\b",
        r"\b3\s+(?:of\s+)?us\b", r"\bthree\s+(?:of\s+)?us\b",
        r"\ball\s+of\s+us\b",
        r"\b3\s+people\b", r"\bthree\s+people\b",
        # Bringing a friend/mate (not partner)
        r"\bbring\s+(?:a|my)\s+(?:friend|mate)\b",
        r"\bbringing\s+(?:a|my)\s+(?:friend|mate)\b",
        r"\bwith\s+(?:a|my)\s+(?:friend|mate)\b",
        r"\bmy\s+mate\s+and\s+(?:i|me)\b",
        # Contextual "doubles" — require explicit booking/session context (not "double room" etc.)
        r"\bdoubles?\s+(?:booking|session|experience|threesome)\b",
        r"\b(?:book(?:ing)?|want|have)\s+a?\s*doubles?\b",
        r"\bdouble\s+(?:the\s+)?(?:girl|lady|escort|booking)\b",
        # Contextual "trio" — require sexual/booking co-signal
        r"\btrio\b.*\b(?:booking|session|girls?|guys?|book|threesome)\b",
        r"\b(?:booking|session|book|threesome)\b.*\btrio\b",
        # Contextual "dp" — require sexual co-signal (not "deposit payment", "dp link" etc.)
        r"\bdp\b.*\b(?:sex|fuck|penetration|anal|inside|both|session|threesome)\b",
        r"\b(?:sex|fuck|penetration|anal|inside|both|threesome)\b.*\bdp\b",
    ],
    "overnight_enquiry": [
        r"\bovernight\b", r"\ball night\b", r"\bsleep.*over\b",
        r"\b24.*hour\b", r"\bwhole night\b",
        r"\bdirty weekend\b", r"\bweekend booking\b", r"\b48.*hour\b",
        r"\bfly\s*me\s*to\s*you\b", r"\bfmty\b", r"\bfly\s*me\s*out\b",
        r"\bfly you (to|out)\b", r"\btravel.*with\s*(?:you|her)\b",
        r"\byou.*fly\b",
    ],
    "msog_enquiry": [
        r"^msog\??$", r"\bmsog\b", r"\bm\.?s\.?o\.?g\.?\b",
        r"\bmultiple shots?\b", r"\bcum twice\b", r"\bgo again\b",
        r"\bmultiple rounds?\b", r"\bcum more than once\b"
    ],

    # BOOKING - includes today-booking intent phrases
    "book_appointment": [
        r"\b(book|see you|when can i)\b",
        r"\bbooking\b", r"\bappointment\b",
        r"\b(tomorrow|tonight|today)\b.*\b(hour|hr|pm|am)\b",
        r"\b\d{1,2}(pm|am)\b",
        r"\bmake.*booking\b", r"\bschedule\b",
        r"\bhmu\b",  # hit me up
        r"\b(next week|this weekend|next weekend)\b",
        # TODAY BOOKING TRIGGERS - phrases indicating want to book for TODAY
        r"\bwhat are you (up to|up 2|doing\??)\b",  # "what are you up to?", "what are you doing?"
        r"\bcan i see you\b.*(today|now|asap)\b",  # "can i see you today?"
        r"\bcan you see me\b.*(today|now|asap)\b",  # "can you see me today?"
        r"\bcome (and )?see me\b",  # "can you come see me", "come and see me"
        r"\bare you (available|free|working)\b.*\btoday\b",  # "are you available today?"
        r"\byou.*\b(available|free|working)\b.*\btoday\b",  # "you available today?"
    ],
    # QUICK BOOKING SHORTCUTS
    "quick_booking": [
        r"\bbook.*(my|the).*(usual|regular|normal)\b",
        r"\b(my|the).*(usual|regular|normal).*booking\b",
        r"\bsame.*(as|like).*last.*(time|booking)\b",
        r"\brepeat.*last.*(time|booking)\b",
        r"\bnext.*available\b",
        r"\bearliest.*available\b",
        r"\bsoonest.*available\b",
        r"\bwhen.*next.*available\b"
    ],

    # FIELD PROVISION (date/time/duration provided)
    "provide_field": [
        r"\b(tomorrow|tonight|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b\d{1,2}(pm|am|:)\b",  # Time
        r"\b\d+\s*(hours?|hrs?|minutes?|mins?|h)\b",  # Duration (plural + singular)
        r"\b(d?gfe|pse)\b",  # Experience type (GFE, DGFE, PSE)
        r"\b(incall|outcall)\b",
        r"\b\d{1,2}[/-]\d{1,2}\b"  # Date format
    ],

    # ENQUIRIES
    "request_outcall": [
        r"\boutcall\b", r"\bout.?call\b",
        r"\bmy (place|hotel|address|location)\b",
        r"\bcome to (me|my)\b", r"\bcome over\b",
        r"\bcome (and )?see me\b", r"\bsee me\b",
        r"\bvisit me\b"
    ],
    "location_enquiry": [
        r"\bwhere.*located\b", r"\bwhat.*location\b",
        r"\bwhich (hotel|city|suburb)\b", r"\bwhere.*you\b"
    ],
    "touring_inquiry": [
        # Touring Australia inquiries - when will you tour, when are you coming back, etc.
        r"\bwhen.*(tour|coming back|visiting|available).*\b(city|sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast)\b",
        r"\bwill you (tour|come back|return|visit).*\b(sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast|city)\b",
        r"\b(are you|do you).*touring.*\b(sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast)\b",
        r"\bwhen.*\b(sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast)\b.*again\b",
        r"\bwhen are you coming back\b",
        r"\bwhen will you be (touring|visiting|back)\b",
        r"\bwhen will you tour\b",
        r"\bare you (touring|traveling|traveling)\b",
        r"\btour (schedule|dates|plan)\b",
        r"\btraveling\b.*\b(this year|soon|next)\b",
        r"\bare you (back|returning|coming back).*\b(sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast)\b",
        r"\b(back in|returning to|visiting).*\b(sydney|melbourne|brisbane|perth|adelaide|hobart|canberra|gold coast)\b",
    ],
    "touring_subscribe": [
        # Client replying TOURING to subscribe to city arrival notifications
        r"^touring$",
        r"^yes.*notify\b",
        r"^notify me\b",
        r"^yes.*touring\b",
        r"^touring.*notify\b",
    ],
    "service_inquiry": [
        r"\bwhat.*services?\b", r"\bwhat.*do you (do|offer)\b",
        r"\bwhat.*can.*you\b", r"\bwhat.*included\b"
    ],

    # SOCIAL
    "greeting": [
        r"^hi+$", r"^hello+$", r"^hey+$", r"^good (morning|afternoon|evening)$",
        r"^hi there$", r"^hey there$", r"^hello there$",
        r"^g'?day$", r"^yo$", r"^sup$", r"^what'?s up$",
        r"\U0001F44B"  # \U0001F44B wave emoji
    ],
    "flirt": [
        r"\bhot\b", r"\bsexy\b", r"\bbeautiful\b", r"\bgorgeous\b",
        r"\bstunning\b", r"\bamazing\b.*\bphotos?\b",
        # Explicit sexual act requests (should be treated as flirting/banter, not service/outcall)
        r"\bdeepthroat\b", r"\bdeep throat\b",
        r"\bblowjob\b", r"\bblow job\b", r"\bhead\b.*\bgive\b", r"\bgive.*\bhead\b",
        r"\bhandjob\b", r"\bhand job\b",
        r"\bsuck\s+my\b", r"\bsuck me\b", r"\bsucking\b",
        r"\bfuck\s+me\b", r"\bfuck you\b", r"\bwanna\s+fuck\b", r"\bwant\s+to\s+fuck\b",
        r"\bcum\s+(on|in|for)\b", r"\bsquirt\b",
        r"\brim\b.*\bme\b", r"\brim me\b",
        r"\bride\s+me\b", r"\bsit\s+on\s+my\b",
        r"\bcock\b", r"\bdick\b", r"\bcum\b",
        r"\bpussy\b", r"\bass\s+on\b",
        r"\bwhat\s+are\s+your\s+services\b",
    ],
    "goodbye": [
        r"\bbye\b", r"\bgoodbye\b", r"\bsee you\b", r"\btalk soon\b",
        r"\bcatch.*later\b", r"\btake care\b"
    ],

    # TIMEWASTERS
    "timewaster": [
        r"\bfirst.*time.*free\b",
        r"\bsample\b", r"\btrial\b", r"\btest.*drive\b"
    ],
    
    # LINK REQUESTS
    "resend_link": [
        r"\bresend\b.*\blink\b", r"\bsend.*link\b", r"\b(?:upload|webform|booking).*link\b",
        r"\blink.*again\b", r"\bnew.*link\b", r"\bget.*link\b"
    ]
}

_COMPILED_DOUBLES_MMF_PATTERNS = _compile_patterns(_DOUBLES_MMF_PATTERNS)
_COMPILED_DOUBLES_MFF_PATTERNS = _compile_patterns(_DOUBLES_MFF_PATTERNS)
_COMPILED_DOUBLES_AMBIGUOUS_PATTERNS = _compile_patterns(_DOUBLES_AMBIGUOUS_PATTERNS)
_COMPILED_DOUBLES_BOOKING_CONTEXT_PATTERNS = _compile_patterns(_DOUBLES_BOOKING_CONTEXT_PATTERNS)
_COMPILED_COUPLES_PARTNER_PATTERNS = _compile_patterns(_COUPLES_PARTNER_PATTERNS)
COMPILED_INTENT_RULES = {intent: _compile_patterns(patterns) for intent, patterns in INTENT_RULES.items()}

# Explicit sexual act language — always route to flirt, never to request_outcall or service_inquiry
_EXPLICIT_SEXUAL_ACT_RE = re.compile(
    r"\b(?:deepthroat|deep\s+throat|blowjob|blow\s+job|handjob|hand\s+job|"
    r"suck\s+(?:my|me|it|that|your)|sucking|"
    r"fuck\s+(?:me|you|my|us)|wanna\s+fuck|want\s+to\s+fuck|fucking\s+you|"
    r"cum\s+(?:on|in|for|inside)|squirt(?:ing)?|"
    r"rim\s+(?:me|you)|rimjob|rim\s+job|"
    r"ride\s+(?:me|my|you)|sit\s+on\s+my|"
    r"\bcock\b|\bdick\b|\bpussy\b|"
    r"anal\b|ass\s+fuck|butt\s+fuck|"
    r"jerk\s+(?:me|you)\s+off|jerk\s+off|wank(?:ing|er)?\b)\b",
    re.IGNORECASE,
)


class Classifier:
    """Intent classifier using pattern matching with AI fallback."""

    def __init__(self, ai_service=None):
        """
        Initialize classifier.

        Args:
            ai_service: Optional AI service for fallback classification
        """
        self.ai_service = ai_service

    def _classify_message_guards(self, message_lower: str, media_urls: list | None) -> str | None:
        if not message_lower:
            logger.info("Intent: other (empty message)")
            return "other"
        if re.fullmatch(r"[^\w]+", message_lower):
            logger.info("Intent: other (punctuation/emoji-only message)")
            return "other"

        # Special case: any MMS with media goes to deposit evaluation — keyword match is a
        # confidence boost only; a screenshot captioned "hi" should still be evaluated.
        if media_urls:
            return "deposit_screenshot"
        return None

    def _classify_safety_intents(self, message_lower: str) -> str | None:
        # Safety-critical intents (must always win before other routing tweaks).
        for safety_intent in ("unsafe_request", "rude_abusive"):
            for pattern in COMPILED_INTENT_RULES.get(safety_intent, []):
                if pattern.search(message_lower):
                    logger.info(f"Intent matched via pattern: {safety_intent}")
                    return safety_intent

        # Explicit sexual act language → always flirt (before any other routing, including AI).
        # Prevents "can you deepthroat my cock?" being misrouted as request_outcall/service_inquiry.
        if _EXPLICIT_SEXUAL_ACT_RE.search(message_lower):
            logger.info("Intent matched via pre-check: flirt (explicit sexual act language)")
            return "flirt"

        # Wrong-number apology — polite exit without pushing booking availability lists.
        if re.search(r"\bwrong number\b", message_lower):
            logger.info("Intent matched via pre-check: wrong_number_opt_out")
            return "wrong_number_opt_out"
        if re.search(
            r"\b(?:stop\s+text(?:ing)?\s+me|do\s+not\s+text(?:\s+me)?|don't\s+text(?:\s+me)?|"
            r"unsubscribe|opt\s*out|remove\s+me|leave\s+me\s+alone|wrong\s+person)\b",
            message_lower,
        ):
            logger.info("Intent matched via pre-check: wrong_number_opt_out (explicit opt-out)")
            return "wrong_number_opt_out"

        # Explicit ENQUIRY keyword (SMS convention for structured questions to the provider).
        if re.match(r"^\s*enquiry\b", message_lower):
            logger.info("Intent matched via pre-check: enquiry_keyword")
            return "enquiry_keyword"

        if re.search(
            r"\b(?:same\s+time\s+as\s+(?:my\s+)?last\s+booking|same\s+as\s+last(?:\s+time|\s+booking)?|"
            r"repeat\s+last(?:\s+time|\s+booking)?|book\s+my\s+usual|next\s+available|earliest|soonest)\b",
            message_lower,
        ):
            logger.info("Intent matched via pre-check: quick_booking")
            return "quick_booking"
        return None

    def _classify_context_state_intents(self, message_lower: str, context: dict[str, Any] | None) -> str | None:
        if not context:
            return None

        st = context.get("state") if isinstance(context, dict) else None
        if st and (st.get("current_state") or "").strip().upper() == "COLLECTING":
            if re.search(
                r"\b(actually|change\s+(?:the\s+)?(?:time|date)|make\s+it|instead\s+(?:do|make|at|for)|"
                r"move\s+(?:it\s+)?to|different\s+(?:time|day|date)|can\s+we\s+change)\b",
                message_lower,
            ) and re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b(?:noon|midnight)\b", message_lower):
                logger.info("Intent matched via pre-check: provide_field (time correction in COLLECTING)")
                return "provide_field"

        if st and (st.get("current_state") or "").strip().upper() == "CONFIRMED":
            if re.search(
                r"\b(reschedule|rescheduled|change\s+(it\s+)?to|move\s+(it\s+)?to|different\s+(time|day|date))\b",
                message_lower,
            ):
                logger.info("Intent matched via pre-check: reschedule (CONFIRMED state)")
                return "reschedule"

        if (
            st
            and (st.get("current_state") or "").strip().upper() == "NEW"
            and not all(st.get(f) for f in ("date", "time", "duration"))
        ):
            _strip = message_lower.strip()
            if _strip in frozenset({"yes", "yep", "yeah", "yup", "ok", "okay", "no"}):
                logger.info("Intent: greeting (bare ack token on NEW without booking fields)")
                return "greeting"
            if re.fullmatch(r"[\U0001f44d\u2705]+", _strip):
                logger.info("Intent: greeting (bare emoji ack on NEW without booking fields)")
                return "greeting"
        return None

    def _classify_special_booking_intents(self, message_lower: str, context: dict[str, Any] | None) -> str | None:
        if _DOUBLE_BOOKING_SLOT_RE.search(message_lower):
            logger.info("Intent matched via pre-check: book_appointment (double-booking slot language)")
            return "book_appointment"

        # Doubles-specific signal detection (explicit MMF/MFF or ambiguous threesome language).
        doubles_signal = classify_doubles_signal(message_lower)
        if doubles_signal in ("mmf_explicit", "mff_explicit", "ambiguous_threesome"):
            logger.info(f"Intent matched via doubles signal: doubles_enquiry ({doubles_signal})")
            return "doubles_enquiry"

        # Overnight dominant signals fire BEFORE dinner pre-check so "all night Friday for dinner"
        # routes to overnight_enquiry rather than being swallowed by the dinner pre-check.
        _OVERNIGHT_DOMINANT_RE = re.compile(
            r"\b(overnight|all night|whole night|24\s*hours?|dirty weekend|fly\s*me\s*to\s*you|fmty)\b",
            re.IGNORECASE,
        )
        if _OVERNIGHT_DOMINANT_RE.search(message_lower):
            logger.info("Intent matched via pre-check: overnight_enquiry")
            return "overnight_enquiry"

        # Hybrid detector (ambiguity-only): run only when broad special-booking language appears
        # but deterministic overnight pre-check did not already match.
        if _looks_like_special_booking_candidate(message_lower):
            try:
                from services.hybrid_nlp_detector import HybridNLPDetector

                state_for_hint = context.get("state") if isinstance(context, dict) else {}
                history_for_hint = context.get("message_history") if isinstance(context, dict) else None
                hybrid_special = HybridNLPDetector(ai_service=self.ai_service).detect_special_booking(
                    message=message_lower,
                    state=state_for_hint if isinstance(state_for_hint, dict) else {},
                    history=history_for_hint if isinstance(history_for_hint, list) else None,
                )
                if hybrid_special.accepted and hybrid_special.hint is not None:
                    logger.info(
                        "Intent matched via hybrid special-booking detector: overnight_enquiry "
                        "(booking_type=%s confidence=%.3f)",
                        getattr(hybrid_special.hint, "booking_type", ""),
                        hybrid_special.confidence,
                    )
                    return "overnight_enquiry"
            except Exception as e:
                logger.warning("Hybrid special-booking detection failed: %s", e)
                try:
                    from utils.structured_logging import log_quality_metric

                    log_quality_metric(
                        "classifier_hybrid_special_failed",
                        error_type=type(e).__name__,
                    )
                except Exception:
                    pass
        return None

    def _classify_social_booking_intents(self, message_lower: str, context: dict[str, Any] | None) -> str | None:
        # Couples/dinner must beat generic "available now" copy so special handlers
        # can return their booking-type-specific templates.
        if _matches_any(message_lower, COMPILED_INTENT_RULES.get("couples_booking", [])):
            logger.info("Intent matched via pre-check: couples_booking")
            return "couples_booking"

        # Farewell pre-check — must fire before available_now which catches "soon".
        # Only applies when there is no booking/availability signal in the same message.
        _FAREWELL_PRE_RE = re.compile(
            r"\b(bye|goodbye|see you|see ya|talk soon|talk later|speak soon|catch you later|ttyl|take care|cya)\b",
            re.IGNORECASE,
        )
        _BOOKING_SIGNAL_RE = re.compile(
            r"\b(book|available|free|now|asap|right now|immediately|at \d|pm|am|tonight|tomorrow|when|what time)\b",
            re.IGNORECASE,
        )
        if _FAREWELL_PRE_RE.search(message_lower) and not _BOOKING_SIGNAL_RE.search(message_lower):
            logger.info("Intent matched via pre-check: goodbye (farewell without booking signal)")
            return "goodbye"

        # Dinner pre-check: use ONLY the conservative opener function (dinner/take you out).
        # Broad social words (wine/drinks/meal/restaurant) require a booking co-signal and are
        # handled in the main loop with an extra context guard.
        if _dinner_new_enquiry_opener(message_lower):
            if context:
                st = context.get("state") if isinstance(context, dict) else None
                if _collecting_dinner_booking(st) and (st or {}).get("first_contact_sent"):
                    logger.info(
                        "Intent: provide_field (COLLECTING dinner follow-up; "
                        "skip dinner_date_enquiry pre-check)"
                    )
                    return "provide_field"
            logger.info("Intent matched via pre-check: dinner_date_enquiry (opener)")
            return "dinner_date_enquiry"
        return None

    def _has_specific_booking_time_or_duration(self, text: str) -> bool:
        if not text:
            return False
        if re.search(r"\d{1,2}\s*:\s*\d{2}\s*(pm|am)\b|\b\d{1,2}\d{0,2}\s*(pm|am)\b", text):
            return True
        if re.search(r"\b(0?[1-9]|1[0-2])\d{2}\b", text):
            return True
        if re.search(r"\b(for\s+)?\d+\s*(hour|hr|h|minute|min)\b", text):
            return True
        if re.search(r"\bat\s+\d{1,2}\b", text):
            return True
        return False

    def _classify_dinner_pattern_intent(
        self,
        message_lower: str,
        context: dict[str, Any] | None,
        pattern: re.Pattern[str],
    ) -> str | None:
        if context:
            st = context.get("state") if isinstance(context, dict) else None
            if (
                _collecting_dinner_booking(st)
                and (st or {}).get("first_contact_sent")
                and not _dinner_new_enquiry_opener(message_lower)
            ):
                logger.info(
                    "Intent: provide_field (COLLECTING dinner follow-up; "
                    "skip dinner_date_enquiry pattern e.g. 'restaurant' in venue name)"
                )
                return "provide_field"

        _SOCIAL_ONLY_RE = re.compile(
            r"^\s*\b(wine|drinks|meal|rest(?:aurant|urant|uraunt))\b", re.IGNORECASE
        )
        if _SOCIAL_ONLY_RE.search(pattern.pattern or ""):
            _DINNER_CO_SIGNAL = re.compile(
                r"\b(dinner|date|take you|book(?:ing)?|reservation|reserve)\b",
                re.IGNORECASE,
            )
            if not _DINNER_CO_SIGNAL.search(message_lower):
                return None
        return "dinner_date_enquiry"

    def _classify_book_appointment_pattern_intent(self, message_lower: str) -> str | None:
        if "see you" in message_lower:
            _FAREWELL_RE = re.compile(
                r"\b(bye|goodbye|later|around|soon|next time|take care|ttyl|cya|see\s+you\s+then)\b"
            )
            if _FAREWELL_RE.search(message_lower) and "book" not in message_lower:
                return None
        return "book_appointment"

    def _classify_confirm_booking_pattern_intent(
        self,
        message_lower: str,
        context: dict[str, Any] | None,
    ) -> str | None:
        if context:
            st = context.get("state") if isinstance(context, dict) else None
            _CONFIRM_PHRASES = frozenset({
                "yes confirmed", "confirmed", "lock it in", "sounds good",
                "let's do it", "lets do it", "book it", "do it",
                "yeah confirmed", "yep confirmed", "all good", "no worries",
                "perfect", "great", "awesome",
            })
            if (
                st
                and (st.get("current_state") or "").strip().upper() == "COLLECTING"
                and not all(st.get(f) for f in ("date", "time", "duration"))
                and (len(message_lower) <= 6 or message_lower.strip() in _CONFIRM_PHRASES)
            ):
                logger.info("Intent: provide_field (confirm phrase in COLLECTING with missing fields)")
                return "provide_field"
        return "confirm_booking"

    def _classify_pattern_override_intent(
        self,
        intent: str,
        pattern: re.Pattern[str],
        message_lower: str,
        context: dict[str, Any] | None,
    ) -> str | None:
        if intent == "dinner_date_enquiry":
            return self._classify_dinner_pattern_intent(message_lower, context, pattern)
        if intent == "book_appointment":
            return self._classify_book_appointment_pattern_intent(message_lower)
        if intent == "confirm_booking":
            return self._classify_confirm_booking_pattern_intent(message_lower, context)
        return intent

    def _classify_pattern_intents(self, message_lower: str, context: dict[str, Any] | None) -> str | None:
        for intent, patterns in COMPILED_INTENT_RULES.items():
            if intent in ("unsafe_request", "rude_abusive"):
                continue
            for pattern in patterns:
                if pattern.search(message_lower):
                    matched_intent = self._classify_pattern_override_intent(
                        intent,
                        pattern,
                        message_lower,
                        context,
                    )
                    if matched_intent is None:
                        continue
                    if matched_intent == intent:
                        logger.info(f"Intent matched via pattern: {intent}")
                    return matched_intent
        return None

    def _classify_ai_intent(self, message_lower: str, context: dict[str, Any] | None) -> str | None:
        # Fallback to AI if available (skip when admin setting "templates first" is on)
        try:
            from core.settings_manager import get_setting
            if (get_setting("ai_templates_first") or "").lower() == "true":
                logger.info("Intent: other (templates-first mode, no pattern match)")
                return "other"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

        if self.ai_service:
            try:
                # Build a rich context hint for the AI classifier
                booking_hint = ""
                history = None
                if context:
                    state = context.get("state", {}) or {}
                    cur_state = state.get("current_state", "NEW")
                    history = context.get("message_history") or None

                    # Collected booking fields
                    fields = {
                        k: state.get(k)
                        for k in ("date", "time", "duration", "incall_outcall", "outcall_address")
                        if state.get(k)
                    }

                    hint_parts = [f"Conversation state: {cur_state}."]

                    if fields:
                        hint_parts.append(
                            "Already collected: "
                            + ", ".join(f"{k}={v}" for k, v in fields.items()) + "."
                        )

                    # Bot's most recent reply gives crucial context for ambiguous messages
                    # (e.g. "yes" / "that one" / "30 mins" after the bot asked a question)
                    if history:
                        last_bot = next(
                            (h["content"] for h in reversed(history) if h.get("role") == "assistant"),
                            None,
                        )
                        if last_bot:
                            hint_parts.append(f'Bot last said: "{last_bot[:120]}".')

                    if cur_state == "COLLECTING":
                        hint_parts.append(
                            "A short reply like '30 mins', '1 hour', 'an hour' is providing duration (provide_field)."
                        )

                    booking_hint = " ".join(hint_parts)

                ai_intent = self.ai_service.classify_intent(
                    message_lower,
                    INTENTS_FOR_LLM,
                    hint=booking_hint,
                    history=history,
                    intent_descriptions=INTENT_DESCRIPTIONS,
                )
                if ai_intent and ai_intent != "other":
                    logger.info(f"Intent classified via AI: {ai_intent}")
                    return ai_intent
            except Exception as e:
                logger.warning(f"AI classification failed: {e}")
                try:
                    from utils.structured_logging import log_quality_metric

                    log_quality_metric(
                        "classifier_ai_fallback_failed",
                        error_type=type(e).__name__,
                    )
                except Exception:
                    pass
        return None

    def classify(self, message: str, media_urls: list[Any] | None = None, context: dict[str, Any] | None = None) -> str:
        """
        Classify message intent.

        Args:
            message: Message text
            media_urls: List of media URLs (for deposit screenshots)
            context: Additional context (current state, etc.)

        Returns:
            Intent string
        """
        media_urls = media_urls or []
        context = context or {}
        message_lower = message.lower().strip()

        intent = self._classify_message_guards(message_lower, media_urls)
        if intent is not None:
            return intent

        intent = self._classify_safety_intents(message_lower)
        if intent is not None:
            return intent

        intent = self._classify_context_state_intents(message_lower, context)
        if intent is not None:
            return intent

        intent = self._classify_special_booking_intents(message_lower, context)
        if intent is not None:
            return intent

        intent = self._classify_social_booking_intents(message_lower, context)
        if intent is not None:
            return intent

        intent = self._classify_pattern_intents(message_lower, context)
        if intent is not None:
            return intent

        intent = self._classify_ai_intent(message_lower, context)
        if intent is not None:
            return intent

        logger.info("Intent: other (no match)")
        return "other"

    def is_safety_intent(self, intent: str) -> bool:
        """Check if intent is safety-critical."""
        return intent in ["unsafe_request", "rude_abusive"]

