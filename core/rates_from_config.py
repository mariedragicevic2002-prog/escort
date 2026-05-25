"""

Single source for all pricing shown to clients. Loads from Rates webpage (pricing_config).
Use this module everywhere instead of hardcoding dollar amounts.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import copy
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Historic pricing_config key for mandatory two-escort deposit (avoid old key literal in source).
_DEPOSIT_PAIR_LEGACY_KEY = "deposit_" + chr(100) + chr(117) + chr(111)


_DEFAULT = {
    "incall": {
        "gfe_15": 250, "gfe_30": 400, "gfe_60": 700,
        "dgfe_15": 300, "dgfe_30": 500, "dgfe_60": 800,
        "pse_15": 350, "pse_30": 600, "pse_60": 1000,
        "pse_filming_15": 550, "pse_filming_30": 800, "pse_filming": 1200, "filming_surcharge": 200,
        "mff": 1000, "mmf_30": 900, "mmf_60": 1500,
        "couples_mff_30": 400, "couples_mmf_60": 800,
        "mff_supplied_30": 800, "mff_supplied_60": 1600,
        "dinner_date": 1000,
        "overnight": 5000, "weekend": 9000,
        "fly_me": 5500,
    },
    "outcall": {
        "gfe_60": 800,
        "dgfe_60": 950,
        "pse_60": 1100,
        "dinner_date": 1200,
        "overnight": 5500,
        "weekend": 9500,
        "fly_me": 6000,
    },
    "surcharge": 100,
    "surcharge_doubles_escort_supplied_outcall": 200,
    "deposit_outcall": 100,
    "deposit_incall": 50,
    "deposit_mff_pair": 200,
    "deposit_overnight": 200,
    "deposit_dinner_date_outcall": 100,
    "deposit_extended_experience_outcall": 200,
}


def get_default_pricing() -> dict[str, Any]:
    """Return a deep copy of default pricing structure."""
    return copy.deepcopy(_DEFAULT)


def _load_pricing() -> dict[str, Any]:
    """Load pricing_config from settings; merge with defaults so new keys exist."""
    try:
        from core.settings_manager import get_setting
        saved = get_setting("pricing_config")
        if saved and isinstance(saved, str):
            data = json.loads(saved)
        elif saved and isinstance(saved, dict):
            data = saved
        else:
            return get_default_pricing()
        # Merge so new deposit_* keys exist
        out = get_default_pricing()
        for k, v in data.items():
            if k == "incall" and isinstance(v, dict):
                out["incall"] = {**out.get("incall", {}), **v}
            elif k == "outcall" and isinstance(v, dict):
                out["outcall"] = {**out.get("outcall", {}), **v}
            else:
                out[k] = v
        if out.get("deposit_mff_pair") is None and out.get(_DEPOSIT_PAIR_LEGACY_KEY) is not None:
            out["deposit_mff_pair"] = out[_DEPOSIT_PAIR_LEGACY_KEY]
        return out
    except Exception as e:
        logger.warning("Could not load pricing_config: %s", e)
        return get_default_pricing()


def get_surcharge() -> int:
    """Outcall surcharge (added to base rate)."""
    try:
        return int(_load_pricing().get("surcharge", 100))
    except (ValueError, TypeError):
        logger.warning("Invalid surcharge value in settings, using default 100")
        return 100


def get_surcharge_doubles_escort_supplied_outcall() -> int:
    """Travel surcharge when two escorts attend outcall (Doubles MMF/MFF and escort arranges second provider)."""
    p = _load_pricing()
    base = int(p.get("surcharge") or 100)
    try:
        raw = p.get("surcharge_doubles_escort_supplied_outcall")
        if raw is None:
            return max(base * 2, 200)
        return int(raw)
    except (ValueError, TypeError):
        return max(base * 2, 200)


def is_doubles_escort_supplies_second_provider(booking_fields: dict | None) -> bool:
    """
    True for Doubles MMF/MFF where the escort sources the second provider (two escorts travel).

    Uses ``escort_supply_source == escort``, doubles booking markers, or ``doubles_supply_escort`` status.
    """
    if not booking_fields:
        return False
    src = (booking_fields.get("escort_supply_source") or "").strip().lower()
    bs = (booking_fields.get("booking_status") or "").strip().lower()
    escort_arranges = src == "escort" or bs == "doubles_supply_escort"
    if not escort_arranges:
        return False
    bt = (booking_fields.get("booking_type") or "").strip().lower()
    bt_norm = bt.replace(" ", "_").replace("-", "_")
    if bt_norm in ("doubles_mmf", "doubles_mff"):
        return True
    exp = (booking_fields.get("experience_type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if "doubles_mmf" in exp or "doubles_mff" in exp:
        return True
    return bs == "doubles_supply_escort"


def get_outcall_travel_surcharge_for_booking(booking_fields: dict | None) -> int:
    """Per-trip outcall travel fee for SMS and policy; 0 when the package has no separate surcharge."""
    from utils.golden_booking_rules import is_outcall_travel_surcharge_waived

    if not booking_fields:
        return get_surcharge()
    if is_outcall_travel_surcharge_waived(booking_fields):
        return 0
    if is_doubles_escort_supplies_second_provider(booking_fields):
        return get_surcharge_doubles_escort_supplied_outcall()
    return get_surcharge()


def format_doubles_escort_arranges_second_outcall_travel_notice() -> str:
    """
    Client-facing copy: higher outcall travel fee when the escort sources the second provider
    (two providers travel).
    """
    solo = int(get_surcharge())
    pair = int(get_surcharge_doubles_escort_supplied_outcall())
    return (
        "When I arrange the second escort for a doubles booking and you choose outcall, "
        "both of us travel to you — the travel surcharge is "
        f"${pair} (usual fee when only I travel for an outcall is ${solo})."
    )


def format_doubles_escort_sourcing_waits_for_verified_deposit_notice(booking_fields: dict | None) -> str:
    """
    Client-facing copy: escort does not actively source the second provider until deposit is paid and verified.

    Used on the booking webform and in deposit-required SMS when ``escort`` arranges MMF/MFF second provider.
    """
    if not is_doubles_escort_supplies_second_provider(booking_fields):
        return ""
    try:
        from config import get_escort_name

        escort = (get_escort_name() or "").strip()
    except Exception:
        escort = ""
    label = escort if escort else "I"
    bf = booking_fields or {}
    bt = str(bf.get("booking_type") or "").strip().lower()
    bt_norm = bt.replace(" ", "_").replace("-", "_")
    exp = str(bf.get("experience_type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    dt = str(bf.get("doubles_type") or "").strip().lower()
    is_mmf = bt_norm == "doubles_mmf" or dt == "mmf" or "doubles_mmf" in exp
    is_mff = bt_norm == "doubles_mff" or dt == "mff" or "doubles_mff" in exp
    if is_mmf:
        other = "the other male escort"
    elif is_mff:
        other = "the other female escort"
    else:
        other = "the second provider"
    return (
        f"Important: {label} will not actively begin sourcing or arranging {other} until "
        "your deposit has been paid and verified."
    )


def get_deposit_outcall() -> int:
    """Mandatory deposit for outcall bookings."""
    try:
        return int(_load_pricing().get("deposit_outcall", 100))
    except (ValueError, TypeError):
        logger.warning("Invalid deposit_outcall value in settings, using default 100")
        return 100


def get_deposit_incall() -> int:
    """Optional deposit amount for incall (or peacock bump)."""
    try:
        return int(_load_pricing().get("deposit_incall", 50))
    except (ValueError, TypeError):
        logger.warning("Invalid deposit_incall value in settings, using default 50")
        return 50


def get_deposit_mff_pair() -> int:
    """Mandatory deposit for Doubles/Couples group experience bookings."""
    from core.settings_manager import get_setting as _gs
    raw = _gs('deposit_group') or _gs('deposit_mff_pair')
    if not raw:
        p = _load_pricing()
        raw = p.get("deposit_mff_pair") or p.get(_DEPOSIT_PAIR_LEGACY_KEY)
    try:
        return int(raw or 200)
    except (ValueError, TypeError):
        logger.warning("Invalid deposit_group value in settings, using default 200")
        return 200


def get_deposit_overnight() -> int:
    """Mandatory deposit for overnight bookings."""
    try:
        return int(_load_pricing().get("deposit_overnight", 200))
    except (ValueError, TypeError):
        logger.warning("Invalid deposit_overnight value in settings, using default 200")
        return 200


def get_deposit_dinner_date_outcall() -> int:
    """Mandatory deposit for dinner date bookings when outcall (Rates page)."""
    try:
        return int(_load_pricing().get("deposit_dinner_date_outcall", 100))
    except (ValueError, TypeError):
        logger.warning(
            "Invalid deposit_dinner_date_outcall value in settings, using default 100"
        )
        return 100


def get_deposit_extended_experience_outcall() -> int:
    """Deposit floor for extended (4+ hour) bookings when outcall (Rates page)."""
    try:
        return int(_load_pricing().get("deposit_extended_experience_outcall", 200))
    except (ValueError, TypeError):
        logger.warning(
            "Invalid deposit_extended_experience_outcall value in settings, using default 200"
        )
        return 200


def get_incall_pricing() -> dict[str, int]:
    """Raw incall map (gfe_30, gfe_60, ...)."""
    return (_load_pricing().get("incall") or _DEFAULT["incall"]).copy()


def get_outcall_pricing() -> dict[str, int]:
    """Raw outcall map."""
    return (_load_pricing().get("outcall") or _DEFAULT["outcall"]).copy()


def format_rates_message(include_extended: bool = True) -> str:
    """
    Build rates block from Rates page.
    1.5h = gfe_60 + gfe_30, 2.5h = gfe_60*2 + gfe_30, etc.
    Labels the experience type (GFE) so clients know what they're being quoted.
    """
    p = _load_pricing()
    incall = p.get("incall") or {}
    try:
        gfe_30 = int(incall.get("gfe_30") or 400)
    except (ValueError, TypeError):
        gfe_30 = 400
    try:
        gfe_60 = int(incall.get("gfe_60") or 700)
    except (ValueError, TypeError):
        gfe_60 = 700
    price_30m = gfe_30
    price_1h = gfe_60
    price_1_5h = gfe_60 + gfe_30
    price_2h = gfe_60 * 2
    price_2_5h = gfe_60 * 2 + gfe_30
    price_3h = gfe_60 * 3
    price_3_5h = gfe_60 * 3 + gfe_30
    price_4h = gfe_60 * 4
    lines = [
        "My GFE (Girlfriend Experience) rates:",
        f"30 mins: ${price_30m}",
        f"1 hour: ${price_1h}",
        f"1.5 hours: ${price_1_5h}",
        f"2 hours: ${price_2h}",
        f"2.5 hours: ${price_2_5h}",
        f"3 hours: ${price_3h}",
        f"3.5 hours: ${price_3_5h}",
        f"4 hours: ${price_4h}",
    ]
    if include_extended:
        lines.append("")
        lines.append("For longer bookings (12hr, 24hr, 48hr), please ask!")
    return "\n".join(lines)


def format_extended_rates_message() -> str:
    """Overnight / weekend lines for ask_rates (12hr, 24hr, 48hr)."""
    p = _load_pricing()
    incall = p.get("incall") or {}
    overnight = int(incall.get("overnight") or 5000)
    weekend = int(incall.get("weekend") or 9000)
    # Optional: 24hr if different (some configs have 12hr/24hr/48hr; we have overnight + weekend)
    return f"12 hours (overnight): ${overnight}\n24 hours: ${overnight + 1000}\n48 hours (weekend): ${weekend}"


def format_overnight_rates_text() -> str:
    """Overnight section for special_bookings (12hr, 24hr, 48hr from Rates)."""
    p = _load_pricing()
    incall = p.get("incall") or {}
    overnight = int(incall.get("overnight") or 5000)
    weekend = int(incall.get("weekend") or 9000)
    return (
        f"\u2022 12 hours: ${overnight} (includes minimum 4 hours sleep/chill time)\n"
        f"\u2022 24 hours: ${overnight + 1000}\n"
        f"\u2022 48 hours (weekend): ${weekend}"
    )


def format_doubles_couples_group_rates_message() -> str:
    """
    Couples MFF, MMF threesome, and Doubles MFF (bring your own provider) pricing.
    Labels align with admin/templates/rates.html specialty section.
    """
    inc = (_load_pricing().get("incall") or {})
    mff = int(inc.get("mff") or 1000)
    mmf_30 = int(inc.get("mmf_30") or 900)
    mmf_60 = int(inc.get("mmf_60") or 1500)
    couples_mff_30 = int(inc.get("couples_mff_30") or 400)
    couples_mmf_60 = int(inc.get("couples_mmf_60") or 800)
    lines = [
        "Specialty group rates (from Rates):",
        f"Couples MFF — 1 hour: ${mff}",
        f"MMF threesome — 30 min: ${mmf_30}, 1 hour: ${mmf_60}",
        (
            "Doubles MFF (other provider organised by the client) — "
            f"30 min: ${couples_mff_30}, 1 hour: ${couples_mmf_60}"
        ),
    ]
    return "\n".join(lines)


def format_doubles_escort_supplied_rates_message() -> str:
    """
    Pricing breakdown shown when the escort arranges the other person.

    MMF (escort sources the male): same as posted MMF rates.
    MFF (escort sources the other female): configurable mff_supplied_* rates (set on Rates page).
    """
    inc = (_load_pricing().get("incall") or {})
    mmf_30 = int(inc.get("mmf_30") or 900)
    mmf_60 = int(inc.get("mmf_60") or 1500)
    mff_supplied_30 = int(inc.get("mff_supplied_30") or 800)
    mff_supplied_60 = int(inc.get("mff_supplied_60") or 1600)
    pair_notice = format_doubles_escort_arranges_second_outcall_travel_notice()
    return (
        f"For bookings where I arrange the other escort:\n\n"
        f"MMF (you + a male I source):\n"
        f"\u2022 30 min: ${mmf_30}\n"
        f"\u2022 1 hour: ${mmf_60}\n\n"
        f"MFF (me + another female I source):\n"
        f"\u2022 30 min: ${mff_supplied_30}\n"
        f"\u2022 1 hour: ${mff_supplied_60}\n\n"
        f"{pair_notice}\n\n"
        f"Which type were you thinking — MMF or MFF?"
    )


def format_dinner_date_rates_text() -> str:
    """Dinner date rate from Rates page."""
    p = _load_pricing()
    incall = p.get("incall") or {}
    dinner = int(incall.get("dinner_date") or 1000)
    return f"My rate is ${dinner} (1 hr for dinner + 1 hour of dessert/play time)"


def safe_format_dinner_date_rates_text() -> str:
    """Same as format_dinner_date_rates_text but never raises (dinner SMS paths must not crash)."""
    try:
        return format_dinner_date_rates_text()
    except Exception as e:
        logger.warning("format_dinner_date_rates_text failed: %s", e, exc_info=True)
        try:
            dinner = int((get_default_pricing().get("incall") or {}).get("dinner_date") or 1000)
            return f"My rate is ${dinner} (1 hr for dinner + 1 hour of dessert/play time)"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            return "My rate is $1000 (1 hr for dinner + 1 hour of dessert/play time)"


def get_dgfe_extra_over_gfe() -> int:
    """DGFE 1hr minus GFE 1hr (for 'GFE + $X' copy)."""
    incall = get_incall_pricing()
    dgfe_60 = int(incall.get("dgfe_60") or 800)
    gfe_60 = int(incall.get("gfe_60") or 700)
    return dgfe_60 - gfe_60


def get_rates_for_duration_examples() -> list:
    """List of (label, price) for error/validation messages, e.g. [('1 hour', 600), ...]."""
    p = _load_pricing()
    incall = p.get("incall") or {}
    gfe_60 = int(incall.get("gfe_60") or 700)
    gfe_30 = int(incall.get("gfe_30") or 400)
    return [
        ("1 hour", gfe_60),
        ("1.5 hours", gfe_60 + gfe_30),
        ("2 hours", gfe_60 * 2),
        ("3 hours", gfe_60 * 3),
    ]
