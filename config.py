"""
Configuration settings for the new streamlined chatbot.

## Golden rule (operational settings)

**All business and integration settings** (SMS gateways, AI keys, maps/geocoding keys, calendar ID,
URLs, screening toggles, payment display strings, location, rollout, etc.) **must** come from the
PostgreSQL ``admin_settings`` table — read via ``get_setting()`` or the typed getters in this
module. Do **not** add new process-environment fallbacks for those keys.

## Bootstrap & host-only (exceptions)

These are **not** duplicated in ``admin_settings`` for chicken-and-egg or deployment reasons:

- ``DATABASE_URL`` — required to open a connection and load settings.
- ``SECRET_KEY`` — may be set only in the host environment until ``flask_secret_key`` exists in
  the database (see ``main_v2.application`` for applying DB secret after DB init).
- ``DEBUG``, optional ``SENTRY_*``, backup/scheduler paths, and similar **host** tuning.

``.env`` is loaded only so local shells and WSGI hosts can supply ``DATABASE_URL`` and the above;
it is **not** a second source of truth for integration credentials.

## Removed

- ``HTTPSMS_DISABLE_ENV_FALLBACK`` — obsolete; env fallbacks for app settings are gone entirely.
"""

import logging
import os
import sys
import threading

from dotenv import load_dotenv

from core.settings_manager import get_setting, set_setting
from utils.log_sanitize import LOG_SUPPRESSED_FMT

# Define BASE_DIR first
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

# Logger
logger = logging.getLogger("escort_chatbot.config")

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================

DATABASE_URL = (os.getenv('DATABASE_URL') or '').strip()
# Default to .env when host env var is missing (common on some WSGI setups).
if not DATABASE_URL:
    from dotenv import dotenv_values

    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_file):
        env_vars = dotenv_values(env_file)
        DATABASE_URL = (env_vars.get('DATABASE_URL') or '').strip()
        if DATABASE_URL:
            # Keep downstream os.environ lookups consistent with config snapshot.
            os.environ.setdefault("DATABASE_URL", DATABASE_URL)


def get_redis_url() -> str:
    """
    Optional Redis URL for distributed rate limiting (Upstash, Redis Cloud, ElastiCache, etc.).

    Resolution order:
    1. ``redis_url`` key in ``admin_settings`` database table (default source of truth)
    2. ``REDIS_URL`` environment variable / ``.env`` file (legacy/emergency fallback)

    Use ``rediss://`` for TLS (required for Upstash). When unset or unreachable,
    rate limiters fall back to in-process memory (not shared across workers).
    """
    def _fix_tls(url: str) -> str:
        """Upstash and most managed Redis services require TLS (rediss://).
        Auto-upgrade redis:// → rediss:// when the host is a known TLS-only provider."""
        if url.startswith("redis://") and any(h in url for h in ("upstash.io", "redislabs.com", "redis.cloud")):
            return "rediss://" + url[len("redis://"):]
        return url

    # Primary: admin_settings DB
    try:
        v = _strip_setting_val(get_setting("redis_url"))
        if v:
            return _fix_tls(v)
    except Exception as e:
        logger.warning("get_redis_url: %s", e, exc_info=True)
    # Fallback: env var / .env file
    env_url = (os.getenv("REDIS_URL") or "").strip()
    if env_url:
        return _fix_tls(env_url)
    return ""


# Automated PostgreSQL dumps (requires `pg_dump` on PATH; runs inside APScheduler when
# RUN_STARTUP_BACKGROUND_JOBS=true). For hosts without a long-running worker, use
# `python scripts/backup_database.py` from cron instead.
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


AUTO_BACKUP_ENABLED = _env_bool("AUTO_BACKUP_ENABLED", True)
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
BACKUP_RETENTION_COUNT = max(1, min(100, int(os.getenv("BACKUP_RETENTION_COUNT", "7"))))
# Daily backup time (UTC) — avoids DST surprises on servers in unknown local zones
BACKUP_HOUR_UTC = max(0, min(23, int(os.getenv("BACKUP_HOUR_UTC", "3"))))
BACKUP_MINUTE_UTC = max(0, min(59, int(os.getenv("BACKUP_MINUTE_UTC", "15"))))

# Optional external Redis for distributed rate limits (see get_redis_url). Admin settings still use in-memory cache + DB.

# ============================================================================
# CONVERSATION SETTINGS
# ============================================================================

# Default when ``conversation_timeout_hours`` is unset in admin_settings
_DEFAULT_CONVERSATION_TIMEOUT_HOURS = 24


def get_conversation_timeout_hours() -> int:
    """Hours of inactivity before conversation state resets (admin_settings only; default 24)."""
    try:
        raw = get_setting("conversation_timeout_hours") or ""
        if raw and str(raw).strip().isdigit():
            return max(1, min(168, int(str(raw).strip())))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return max(1, min(168, _DEFAULT_CONVERSATION_TIMEOUT_HOURS))

# ============================================================================
# HTTPSMS GATEWAY CONFIGURATION
# ============================================================================
# httpSMS — cloud-based Android phone gateway for outbound/inbound SMS.
# Values are stored in ``admin_settings`` (Config UI).

def _strip_setting_val(raw: str | None) -> str:
    """Normalize DB/setting values (may be non-str from drivers)."""
    if raw is None:
        return ""
    return str(raw).strip()


def _setting_bool(raw: str | None, default: bool = True) -> bool:
    """Parse truthy/falsy values from ``admin_settings`` strings."""
    s = _strip_setting_val(raw).lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def get_httpsms_api_key() -> str:
    """httpSMS API key from ``admin_settings`` (``httpsms_api_key``)."""
    return _strip_setting_val(get_setting("httpsms_api_key"))


def get_httpsms_phone_number() -> str:
    """httpSMS sender phone number (E.164) from ``admin_settings`` (``httpsms_phone_number``)."""
    return _strip_setting_val(get_setting("httpsms_phone_number"))


def httpsms_is_enabled() -> bool:
    """True if httpSMS gateway is enabled (default ON when unset)."""
    v = _strip_setting_val(get_setting("httpsms_enabled"))
    return _setting_bool(v, default=True)


def httpsms_is_configured() -> bool:
    """True if both the httpSMS API key and phone number are set."""
    return bool(get_httpsms_api_key() and get_httpsms_phone_number())


# ============================================================================
# HTTPSMS WEBHOOK SETTINGS
# ============================================================================

def get_httpsms_webhook_secrets() -> list[str]:
    """
    Shared webhook bearer secrets (comma/newline separated) for legacy /webhook auth.

    Returned in priority order so the first value is treated as the primary secret.
    """
    raw = _strip_setting_val(get_setting("httpsms_webhook_secret"))
    if not raw:
        raw = _strip_setting_val(get_setting("httpsms_webhook_secrets"))
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace("\n", ",").split(","):
        secret = chunk.strip()
        if not secret or secret in seen:
            continue
        out.append(secret)
        seen.add(secret)
    return out


def get_httpsms_webhook_secret() -> str:
    """Primary webhook bearer secret (first configured secret)."""
    secrets = get_httpsms_webhook_secrets()
    return secrets[0] if secrets else ""


def get_httpsms_webhook_secret_rotation_config() -> dict[str, str]:
    """Optional active/next/deprecated webhook secret rotation settings."""
    active = _strip_setting_val(get_setting("httpsms_webhook_secret_active")) or get_httpsms_webhook_secret()
    return {
        "active_key": active,
        "next_key": _strip_setting_val(get_setting("httpsms_webhook_secret_next")),
        "deprecated_key": _strip_setting_val(get_setting("httpsms_webhook_secret_deprecated")),
        "cutover_state": _strip_setting_val(get_setting("httpsms_webhook_secret_cutover_state")),
    }


def get_httpsms_webhook_signature_secret() -> str:
    """HMAC secret used to verify timestamped webhook signatures."""
    return _strip_setting_val(get_setting("httpsms_webhook_signature_secret"))


def get_httpsms_webhook_signature_rotation_config() -> dict[str, str]:
    """Optional active/next/deprecated webhook signature secret rotation settings."""
    active = (
        _strip_setting_val(get_setting("httpsms_webhook_signature_secret_active"))
        or get_httpsms_webhook_signature_secret()
    )
    return {
        "active_key": active,
        "next_key": _strip_setting_val(get_setting("httpsms_webhook_signature_secret_next")),
        "deprecated_key": _strip_setting_val(get_setting("httpsms_webhook_signature_secret_deprecated")),
        "cutover_state": _strip_setting_val(get_setting("httpsms_webhook_signature_secret_cutover_state")),
    }


def httpsms_webhook_signature_required() -> bool:
    """Whether inbound webhook signature verification is required.

    Defaults OFF — httpSMS does not send the custom HMAC headers this app
    expects, so signature verification must be explicitly opted in via the
    admin settings page. Set ``httpsms_webhook_signature_required = true``
    in admin settings only if you have a custom relay that sends the
    X-Webhook-Timestamp and X-Webhook-Signature headers.
    """
    raw = _strip_setting_val(get_setting("httpsms_webhook_signature_required"))
    if raw is not None and raw != "":
        return _setting_bool(raw, default=False)
    # Explicit setting not present — default: OFF (httpSMS doesn't use our HMAC scheme).
    return False


def get_httpsms_webhook_signature_tolerance_seconds() -> int:
    """Replay-window tolerance (seconds) for webhook signatures; clamped to [30, 3600]."""
    raw = _strip_setting_val(get_setting("httpsms_webhook_signature_tolerance_seconds"))
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = 300
    return max(30, min(3600, parsed))


# ========================================================================
# SAFETY SCREENING CONFIGURATION
# ========================================================================


def safety_screening_is_enabled() -> bool:
    """
    Whether inbound client safety screening is enabled.

    DB setting key: ``safety_screening_enabled`` (default OFF when unset).
    """
    v = _strip_setting_val(get_setting("safety_screening_enabled"))
    return _setting_bool(v, default=False)


def get_safety_screening_mode() -> str:
    """
    Safety screening action mode.

    Returns ``warn_only`` or ``auto_block`` (default ``warn_only``).
    """
    v = _strip_setting_val(get_setting("safety_screening_mode"))
    if not v:
        v = "warn_only"
    mode = v.lower()
    return "auto_block" if mode == "auto_block" else "warn_only"


def get_escort_phone_number() -> str:
    """Escort mobile number for SMS notifications from ``admin_settings``."""
    try:
        return _strip_setting_val(get_setting("escort_phone_number"))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return ""

# ============================================================================
# AI CONFIGURATION
# ============================================================================


def get_anthropic_api_key() -> str:
    """Claude/Anthropic API key from ``admin_settings`` (``claude_api_key``)."""
    try:
        return _strip_setting_val(get_setting("claude_api_key"))
    except Exception as e:
        logger.warning("get_anthropic_api_key: %s", e, exc_info=True)
        return ""


def get_gemini_api_key() -> str:
    """Gemini API key from ``admin_settings`` (``gemini_api_key``)."""
    try:
        return _strip_setting_val(get_setting("gemini_api_key"))
    except Exception as e:
        logger.warning("get_gemini_api_key: %s", e, exc_info=True)
        return ""


def check_ai_keys_configured() -> bool:
    """Log a warning and return False if neither AI key is configured.

    Call this once after the application and database pool are fully
    initialised (e.g. inside ``main_v2.application`` or the WSGI startup
    sequence) — **not** at module import time, where the DB may not yet be
    reachable.
    """
    if not get_anthropic_api_key() and not get_gemini_api_key():
        logger.warning(
            "No AI API keys configured — save Claude/Gemini keys on the Config page. AI requests will fail."
        )
        return False
    return True

AI_MODEL_CLAUDE = "claude-sonnet-4-6"
AI_MODEL_GEMINI = "gemini-2.5-flash"

AI_TIMEOUT = 25.0
AI_MAX_TOKENS = 512  # Reduced - only for extraction, not generation

# ============================================================================
# GOOGLE SERVICES CONFIGURATION
# ============================================================================


_last_good_calendar_id: str = ""
_last_good_calendar_id_lock = threading.Lock()


def get_google_calendar_id() -> str:
    """
    Google Calendar ID for API calls (``calendar_id`` in ``admin_settings``).

    Caches the last successfully read value so transient DB/SSL errors do not blank the ID mid-process.
    """
    global _last_good_calendar_id
    try:
        v = _strip_setting_val(get_setting("calendar_id"))
    except Exception as e:
        logger.warning("get_google_calendar_id: %s", e, exc_info=True)
        v = ""
    if v:
        with _last_good_calendar_id_lock:
            _last_good_calendar_id = v
        return v
    with _last_good_calendar_id_lock:
        return _last_good_calendar_id


def get_google_maps_server_api_key() -> str:
    """Maps server key from ``admin_settings`` (``google_maps_server_api_key`` or legacy ``google_maps_api_key``)."""
    try:
        v = _strip_setting_val(get_setting("google_maps_server_api_key"))
        if v:
            return v
        legacy = _strip_setting_val(get_setting("google_maps_api_key"))
        if legacy:
            return legacy
    except Exception as e:
        logger.warning("get_google_maps_server_api_key: %s", e, exc_info=True)
    return ""


def get_google_maps_browser_api_key() -> str:
    """Maps browser key from ``admin_settings`` (``google_maps_browser_api_key`` or legacy ``google_maps_api_key``)."""
    try:
        v = _strip_setting_val(get_setting("google_maps_browser_api_key"))
        if v:
            return v
        legacy = _strip_setting_val(get_setting("google_maps_api_key"))
        if legacy:
            return legacy
    except Exception as e:
        logger.warning("get_google_maps_browser_api_key: %s", e, exc_info=True)
    return ""


def get_opencage_api_key() -> str:
    """OpenCage key from ``admin_settings``."""
    try:
        return _strip_setting_val(get_setting("opencage_api_key"))
    except Exception as e:
        logger.warning("get_opencage_api_key: %s", e, exc_info=True)
        return ""


def get_google_calendar_api_key() -> str:
    """Optional field in ``admin_settings``; Google Calendar normally uses OAuth/service account files."""
    try:
        return _strip_setting_val(get_setting("google_calendar_api_key"))
    except Exception as e:
        logger.warning("get_google_calendar_api_key: %s", e, exc_info=True)
        return ""


# Module-level snapshots — intentionally empty strings; always use the getter functions above
# for fresh DB reads at request time. These constants exist only so legacy import statements
# that do `from config import GOOGLE_MAPS_BROWSER_API_KEY` don't crash; callers must be
# migrated to the getter (get_google_maps_browser_api_key(), etc.) to get live values.
GOOGLE_CALENDAR_ID = ""
GOOGLE_MAPS_API_KEY = ""
GOOGLE_MAPS_SERVER_API_KEY = ""
GOOGLE_MAPS_BROWSER_API_KEY = ""
OPENCAGE_API_KEY = ""

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/cloud-vision"
]

OAUTH_CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials_oauth.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "credentials.json") if os.path.isfile(os.path.join(BASE_DIR, "credentials.json")) else None
CREDENTIALS_JSON_ENV = os.environ.get("CREDENTIALS_JSON", "")

USE_OAUTH = os.path.isfile(OAUTH_CREDENTIALS_FILE)
HAS_VISION = bool(SERVICE_ACCOUNT_FILE or CREDENTIALS_JSON_ENV)

# Calendar colors
COLOR_GRAPHITE = "8"   # Grey - Pending deposit
COLOR_PEACOCK = "7"    # Turquoise - Reserved (no deposit required)
COLOR_BASIL = "2"      # Green - Confirmed with deposit
COLOR_GRAPE = "3"      # Purple - Confirmed travel time for outcalls
COLOR_LAVENDER = "1"   # Light purple - Pending travel (soft hold: bookable over; non-blocking in bot + transparent in Calendar)
# Appended to graphite/lavender calendar descriptions; webform /api/booked-times matches this when colorId is missing
ESCORT_CALENDAR_SOFT_HOLD_MARKER = "escort_calendar_soft_hold=1"
# Backward-compat alias for legacy imports not yet renamed.
ADELLA_CALENDAR_SOFT_HOLD_MARKER = ESCORT_CALENDAR_SOFT_HOLD_MARKER
COLOR_BANANA = "5"     # Yellow - Maintenance (hairdressing, nails, etc.); manual admin — blocks public webform like paid bookings
COLOR_TOMATO = "11"   # Red - Social events; manual admin — same blocking as Banana for calendar availability

# ============================================================================
# BUSINESS RULES
# ============================================================================

# Location / profile (fallbacks; prefer get_profile_url() and get_escort_name() from settings)
PROFILE_URL = "https://scarletblue.com.au/escort/escort-allure"
AVAILABLE_HOURS = "3pm-3am, 7 days a week"


def get_escort_name(default: str = "escort") -> str:
    """Escort/business display name from admin settings (used everywhere user-facing). Never raises."""
    try:
        out = get_setting("escort_name", default) or default
        return (out if out and isinstance(out, str) else default) or default
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return default


def get_profile_url() -> str:
    """Profile URL from admin settings (full URL), or fallback to PROFILE_URL. Never raises."""
    try:
        url = get_setting("profile_url") or ""
        if not url or not isinstance(url, str):
            return PROFILE_URL
        url = url.strip()
        if not url:
            return PROFILE_URL
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        return url
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return PROFILE_URL

# Mandatory booking fields
MANDATORY_FIELDS = ['date', 'time', 'duration']

# Today-booking trigger phrases - phrases that indicate client wants to book for TODAY
TODAY_BOOKING_TRIGGERS = [
    "what are you up to", "what are you up 2", "what are you doing",
    "what are you doing", "can i see you today", "can i see you now",
    "are you available today", "are you free today",
    "can you see me today", "can you see me now",
    "are you working today", "you available today"
]

# Time window configuration for date determination
DAYTIME_START_HOUR = 7      # 7 AM - when daytime booking window starts
DAYTIME_END_HOUR = 21       # 9 PM - when daytime booking window ends (21:00)
NIGHTTIME_START_HOUR = 21   # 9 PM - when nighttime booking window starts
NIGHTTIME_END_HOUR = 7      # 7 AM - when nighttime booking window ends (next day)

# Time word lists for parsing
MIDDAY_WORDS = ["midday", "noon", "12pm", "12 pm", "12:00pm", "12:00 pm", "mid-day", "mid day", "lunch time", "lunchtime"]
MIDNIGHT_WORDS = ["midnight", "12am", "12 am", "12:00am", "12:00 am", "mid-night", "mid night"]

# Today words - expanded for better recognition
TODAY_WORDS = [
    "today", "this morning", "this afternoon", "this evening", "this arvo",
    "tonite", "2nite", "2day"  # SMS abbreviations
]

# Tonight words - messages during 9 PM - 7 AM referring to current/same night service
TONIGHT_WORDS = [
    "tonight", "tonite", "2nite", "tonight", "this nite",
    "this night", "now", "asap", "right now"
]

TOMORROW_WORDS = ["tomorrow", "tmrw", "tmr", "tomoz", "2moro", "2morrow"]

# Pricing
RATES = {
    "1hr": 600,
    "1hr_30min": 800,
    "2hr": 1000,
    "2hr_30min": 1200,
    "3hr": 1400,
    "3hr_30min": 1600,
    "4hr": 1800,
    "additional_30min": 200,
    "12hr": 3500,
    "24hr": 6000,
    "48hr": 10000,
}

# Deposit configuration
DEPOSIT_AMOUNT_INCALL = 50
DEPOSIT_AMOUNT_OUTCALL = 100
MAX_DEPOSIT_SCREENSHOT_ATTEMPTS = 3

# ============================================================================
# TIMEZONE CONFIGURATION
# ============================================================================

DEFAULT_TIMEZONE = "Australia/Adelaide"

CITY_TIMEZONES = {
    "adelaide": "Australia/Adelaide",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "darwin": "Australia/Darwin",
    "hobart": "Australia/Hobart",
    "canberra": "Australia/Sydney",
    "gold coast": "Australia/Brisbane",
    # Unambiguous WA names; see get_timezone_for_city for "perth" substring
    "fremantle": "Australia/Perth",
    "subiaco": "Australia/Perth",
    "joondalup": "Australia/Perth",
}

def get_timezone_for_city(city: str) -> str:
    """Get IANA zone from free-text city / suburb; falls back to :data:`DEFAULT_TIMEZONE` when unknown.

    The Location form often stores a suburb (e.g. *Subiaco*) rather than the word *Perth*, so
    we include common Perth-metro names and a ``\"perth\" in city`` fallback (e.g. *(escort_name), Perth*).
    """
    if not city:
        return DEFAULT_TIMEZONE
    c = city.lower().strip()
    if c in CITY_TIMEZONES:
        return CITY_TIMEZONES[c]
    if "perth" in c:
        return "Australia/Perth"
    return DEFAULT_TIMEZONE


def get_effective_escort_timezone() -> str:
    """
    IANA timezone for escort-local business time ("now", slot parsing, reminders).

    Order: ``admin_settings.timezone`` (saved by the Location web tab when you
    save city), else derive from ``city`` via :func:`get_timezone_for_city`,
    else :data:`DEFAULT_TIMEZONE`.
    """
    try:
        tz = _strip_setting_val(get_setting("timezone"))
        if tz:
            return tz
        loc = _strip_setting_val(get_setting("location_timezone"))
        if loc:
            return loc
        city = _strip_setting_val(get_setting("city"))
        if city:
            return get_timezone_for_city(city)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return DEFAULT_TIMEZONE


def get_payid() -> str:
    """PayID from ``admin_settings``."""
    try:
        return _strip_setting_val(get_setting("payid"))
    except Exception as e:
        logger.warning("get_payid: %s", e, exc_info=True)
        return ""


def get_account_name() -> str:
    """Account name from ``admin_settings``."""
    try:
        return _strip_setting_val(get_setting("account_name"))
    except Exception as e:
        logger.warning("get_account_name: %s", e, exc_info=True)
        return ""


def get_available_hours():
    """Get available hours from database settings. Never raises."""
    try:
        out = get_setting('available_hours', AVAILABLE_HOURS)
        return out if (out and isinstance(out, str)) else AVAILABLE_HOURS
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return AVAILABLE_HOURS


def get_pricing():
    """Get pricing configuration."""
    return RATES


def get_current_incall_location():
    """
    Get current incall location from ``admin_settings``.
    Never returns None for string fields.
    """
    try:
        city = _strip_setting_val(get_setting("city"))
        hotel_name = _strip_setting_val(get_setting("hotel_name"))
        address = _strip_setting_val(get_setting("address"))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        city = hotel_name = address = ""
    return {
        "city": city or "",
        "hotel_name": hotel_name or "",
        "address": address or "",
        "display_name": hotel_name or "",
    }


def get_touring_australia():
    """
    Get touring Australia details from database settings (marketing / client notifications).

    Used to tell clients **when** you will visit a city (dates, SMS opt-in, etc.).
    **Not** the source of "where I am now" for bookings — use :func:`get_current_incall_location`
    / :func:`get_effective_booking_city` for that.

    Returns dict with is_touring, tour_start_date, tour_end_date, tour_city, tour_hotel_name, tour_address.
    """
    try:
        return {
            'is_touring': (get_setting('is_touring', '0') or '').strip() == '1',
            'tour_start_date': get_setting('tour_start_date', ''),
            'tour_end_date': get_setting('tour_end_date', ''),
            'tour_city': get_setting('tour_city', ''),
            'tour_hotel_name': get_setting('tour_hotel_name', ''),
            'tour_address': get_setting('tour_address', ''),
        }
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return {
            'is_touring': False,
            'tour_start_date': '',
            'tour_end_date': '',
            'tour_city': '',
            'tour_hotel_name': '',
            'tour_address': '',
        }


def get_effective_booking_city() -> str:
    """
    City name for outcall policy, CBD wording, distance checks, and booking copy.

    **Always** comes from admin **Location** (``city`` / :func:`get_current_incall_location`).
    There is no separate "home base": the escort is always on tour operationally, and
    Location is updated to wherever they are working now.

    The **Touring Australia** block (``tour_city``, dates, etc.) is only for telling
    clients when you will visit *their* city — it must not override Location for
    bookings or 15km rules. Use :func:`get_touring_australia` in touring/notification flows.
    """
    try:
        loc = get_current_incall_location()
        c = (loc.get("city") or "").strip()
        if c:
            return c
        return _strip_setting_val(get_setting("city"))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return ""


def get_cbd_label_for_messages(explicit_city: str | None = None) -> str:
    """
    Human-readable CBD phrase for SMS/templates, e.g. ``Perth CBD``.

    Uses ``explicit_city`` when provided; otherwise the **Location** city from
    :func:`get_effective_booking_city`. If no city is configured, returns a generic phrase.
    """
    c = (explicit_city or "").strip() or get_effective_booking_city()
    if c and c.lower() not in ('the', 'my', 'here'):
        return f"{c} CBD"
    return "the CBD where I'm based"


# Default public site URL when ``base_url`` is unset in ``admin_settings``
DEFAULT_BASE_URL = "https://www.escort-allure.com.au"


def get_base_url() -> str:
    """Public base URL for booking/deposit/feedback links: ``admin_settings.base_url``, else DEFAULT_BASE_URL (no trailing slash)."""
    try:
        base = (get_setting("base_url") or "").strip().rstrip("/")
        if base:
            return base
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return DEFAULT_BASE_URL


def set_location(city: str, hotel_name: str, address: str) -> bool:
    """
    Update location in database settings.

    Args:
        city: City name
        hotel_name: Hotel name
        address: Full address

    Returns:
        True if successful
    """
    try:
        set_setting('city', city)
        set_setting('hotel_name', hotel_name)
        set_setting('address', address)
        logger.info("Location updated: %s - %s", city, hotel_name)
        return True
    except Exception as e:
        logger.error("Error updating location: %s", e)
        return False

# ============================================================================
# ADMIN CONFIGURATION
# ============================================================================

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip() or None
_admin_numbers_raw = os.environ.get("AUTHORIZED_ADMIN_NUMBERS", "").split(",")
AUTHORIZED_ADMIN_NUMBERS = [n.strip() for n in _admin_numbers_raw if n.strip()]

# ============================================================================
# FEATURE FLAGS
# ============================================================================

DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
DEBUG_FLOW = os.getenv("DEBUG_FLOW", "1").strip() not in ("0", "false", "False")
WEBHOOK_FALLBACK_ON_403 = os.getenv("WEBHOOK_FALLBACK_ON_403", "false").strip().lower() in ("1", "true", "yes", "on")


def validate_config() -> None:
    """
    Validate that critical config values are present and not placeholder defaults.
    Call this at application startup. Logs warnings for non-critical issues,
    raises RuntimeError only for values that will definitely break the app.
    """
    errors = []
    warnings = []

    if not DATABASE_URL:
        warnings.append(
            "DATABASE_URL is not set — set it in the Web environment (PythonAnywhere) or .env. "
            "The app will start without a database; admin/SMS features need a valid URL."
        )

    if not get_anthropic_api_key() and not get_gemini_api_key():
        warnings.append(
            "No AI API keys — save Claude/Gemini keys on the Config page (admin_settings)."
        )

    admin_pwd = os.environ.get("ADMIN_PASSWORD", "").strip()
    weak_admin_passwords = {"change-this-password-now", "changeme", "admin", "password"}
    if not admin_pwd or admin_pwd in weak_admin_passwords:
        msg = "ADMIN_PASSWORD is not set or is a weak default — please update before going live"
        warnings.append(msg)

    if DEBUG:
        # PA sets PYTHONANYWHERE_DOMAIN / PYTHONANYWHERE_SITE at runtime;
        # ENVIRONMENT=production is the explicit override operators can set.
        # If any of those are present, DEBUG=True is almost certainly a
        # misconfiguration (stack traces in responses, verbose logs, fail-soft
        # startup paths) — refuse to start rather than warn.
        production_signal = (
            os.environ.get("PYTHONANYWHERE_DOMAIN")
            or os.environ.get("PYTHONANYWHERE_SITE")
            or (os.environ.get("ENVIRONMENT") or "").strip().lower() == "production"
        )
        running_pytest = (
            os.environ.get("PYTEST_RUNNING", "").strip().lower() in {"1", "true", "yes"}
            or "_pytest" in sys.modules
        )
        if production_signal and not running_pytest:
            errors.append(
                "DEBUG=True detected in a production environment "
                "(PYTHONANYWHERE_* or ENVIRONMENT=production). Refusing to start."
            )
        elif not production_signal:
            warnings.append(
                "DEBUG=True is set — set DEBUG=False for production."
            )

    for w in warnings:
        logger.warning("CONFIG WARNING: %s", w)

    if errors:
        raise RuntimeError("Critical config errors: " + "; ".join(errors))
